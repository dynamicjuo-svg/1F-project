"""
app.py — Streamlit 기반 토지 실거래 검색 UI.

테스트 범위: 용인 처인구 백암면 + 원삼면 5년치.

UX 요점:
  · 자연어 한 줄 → 시세 요약 + 지도 + 표
  · 지도: 모든 거래를 핫핑크 마커로 표시. 마커/폴리곤 호버 시 그 필지가
         신뢰도 색으로 강조됨 (평소 폴리곤은 투명).
  · 표: 헤더 클릭으로 정렬 (오름차 ↔ 내림차). 행 클릭 시 그 거래로 지도
       중심·확대·강조. 지도에서 클릭해도 표의 행이 노란색으로 강조됨.
"""

import io
import json
import os
import sqlite3
import statistics
from collections import Counter
from datetime import datetime

import anthropic
import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium

from api_keys import ANTHROPIC_KEY, VWORLD_KEY, NAVER_MAP_CLIENT_ID
from search import parse_query, map_road, point_to_line_m, build_period_range


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
STATIC_DIR = os.path.join(HERE, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
ALLOWED_EMD = {"백암면", "원삼면"}
PYEONG_PER_M2 = 1.0 / 3.3058
SELECTED_COLOR = "#dc2626"
CONF_COLOR = {"high": "#22c55e", "mid": "#f97316", "low": "#9ca3af"}

# V-World 베이스맵 (국토교통부) — 한국 지명·도로 표시에 최적화
VWORLD_TILES = (
    f"https://api.vworld.kr/req/wmts/1.0.0/{VWORLD_KEY}/Base/"
    "{z}/{y}/{x}.png"
)
VWORLD_ATTR = "V-World (국토교통부)"

# 지목별 마커·폴리곤 색상
JIMOK_COLOR = {
    "임야":     "#15803d",  # 짙은 녹색
    "전":       "#a16207",  # 갈색
    "답":       "#ca8a04",  # 황갈색
    "과수원":   "#ec4899",  # 핑크
    "목장용지": "#84cc16",  # 라임
    "대":       "#dc2626",  # 빨강
    "공장용지": "#3b82f6",  # 파랑
    "창고용지": "#a855f7",  # 보라
    "도로":     "#6b7280",  # 회색
    "철도용지": "#404040",  # 진회색
    "잡종지":   "#525252",  # 진회색
    "체육용지": "#06b6d4",  # 청록
    "유지":     "#0ea5e9",  # 청색
    "하천":     "#0284c7",  # 짙은 청색
    "구거":     "#22d3ee",  # 밝은 청록
    "제방":     "#737373",  # 회색
    "공원":     "#16a34a",  # 녹색
    "묘지":     "#171717",  # 검정
    "종교용지": "#7c3aed",  # 보라
    "사적지":   "#7c3aed",  # 보라
    "주차장":   "#737373",
    "주유소용지": "#737373",
    "학교용지": "#0891b2",
    "수도용지": "#0ea5e9",
    "유원지":   "#ec4899",
    "양어장":   "#0ea5e9",
    "광천지":   "#0ea5e9",
    "염전":     "#a78bfa",
    "과수원":   "#ec4899",
}
DEFAULT_JIMOK_COLOR = "#9ca3af"


def jimok_color(jimok: str) -> str:
    return JIMOK_COLOR.get(jimok, DEFAULT_JIMOK_COLOR)


# =====================================================================
#  네이버 지도 HTML 빌더 (streamlit components 임베드)
# =====================================================================
def build_naver_map_html(client_id, center, zoom, markers, polygons,
                          road_lines, height=520):
    """네이버 지도 v3 SDK 임베드 HTML."""
    markers_json = json.dumps(markers, ensure_ascii=False)
    polygons_json = json.dumps(polygons, ensure_ascii=False)
    roads_json = json.dumps(road_lines or [], ensure_ascii=False)
    sel_color = SELECTED_COLOR

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script src="https://oapi.map.naver.com/openapi/v3/maps.js?ncpClientId={client_id}"></script>
<style>
  body, html {{ margin: 0; padding: 0; font-family: system-ui, sans-serif; }}
  #map {{ width: 100%; height: {height}px; }}
  .info-card {{
    padding: 10px 12px; font-size: 12px; line-height: 1.5;
    max-width: 320px; background: white;
    border: 1px solid #d4d4d8; border-radius: 6px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
  }}
</style>
</head>
<body>
<div id="map"></div>
<script>
if (typeof naver === 'undefined' || !naver.maps) {{
  document.getElementById('map').innerHTML =
    '<div style="padding:24px;color:#dc2626;font-size:13px;">'
    + '네이버 지도 SDK 로드 실패. <br><br>'
    + '확인:<br>'
    + '1) NCP 콘솔에서 Maps Application의 "서비스 URL"에 '
    + 'http://localhost:8501 가 등록되어 있는지<br>'
    + '2) Client ID가 정확한지 (Client Secret 아님)<br>'
    + '3) Maps API "Web Dynamic Map" 서비스가 활성화되어 있는지'
    + '</div>';
}} else {{
  const map = new naver.maps.Map('map', {{
    center: new naver.maps.LatLng({center[0]}, {center[1]}),
    zoom: {zoom},
    mapTypeId: naver.maps.MapTypeId.NORMAL,
    zoomControl: true,
    zoomControlOptions: {{ position: naver.maps.Position.TOP_RIGHT }},
    mapTypeControl: true,
    scaleControl: true,
  }});

  // 도로 라인 (검색 대상 도로)
  const roads = {roads_json};
  roads.forEach(line => {{
    const path = line.map(c => new naver.maps.LatLng(c[1], c[0]));
    new naver.maps.Polyline({{
      map: map, path: path,
      strokeColor: '#3b82f6', strokeWeight: 3, strokeOpacity: 0.6,
    }});
  }});

  // 폴리곤 — 평소엔 투명, 호버 시 지목 색으로 강조
  const SEL = '{sel_color}';
  const polygons = {polygons_json};
  polygons.forEach(p => {{
    const paths = p.coords.map(ring =>
      ring.map(c => new naver.maps.LatLng(c[1], c[0]))
    );
    const isSel = p.is_selected;
    const polygon = new naver.maps.Polygon({{
      map: map, paths: paths,
      fillColor: isSel ? SEL : p.color,
      fillOpacity: isSel ? 0.45 : 0,
      strokeColor: isSel ? SEL : '#ffffff',
      strokeOpacity: isSel ? 1 : 0.3,
      strokeWeight: isSel ? 3 : 0.5,
      clickable: true,
    }});

    const info = new naver.maps.InfoWindow({{
      content: '<div class="info-card">' + p.html + '</div>',
      borderWidth: 0, anchorSize: new naver.maps.Size(0, 0),
      pixelOffset: new naver.maps.Point(0, -8),
    }});

    if (!isSel) {{
      naver.maps.Event.addListener(polygon, 'mouseover', e => {{
        polygon.setOptions({{
          fillOpacity: 0.45, strokeColor: p.color,
          strokeOpacity: 1, strokeWeight: 3,
        }});
        info.open(map, e.coord);
      }});
      naver.maps.Event.addListener(polygon, 'mouseout', () => {{
        polygon.setOptions({{
          fillOpacity: 0, strokeColor: '#ffffff',
          strokeOpacity: 0.3, strokeWeight: 0.5,
        }});
        info.close();
      }});
    }} else {{
      naver.maps.Event.addListener(polygon, 'mouseover', e => {{
        info.open(map, e.coord);
      }});
      naver.maps.Event.addListener(polygon, 'mouseout', () => info.close());
    }}
  }});

  // 마커 — 지목별 색, 항상 표시
  const markers = {markers_json};
  markers.forEach(m => {{
    const sel = m.is_selected;
    const size = sel ? 16 : 12;
    const color = sel ? SEL : m.color;
    const marker = new naver.maps.Marker({{
      position: new naver.maps.LatLng(m.lat, m.lon),
      map: map,
      icon: {{
        content:
          '<div style="background:' + color + ';width:' + size + 'px;'
          + 'height:' + size + 'px;border-radius:50%;'
          + 'border:2px solid white;'
          + 'box-shadow:0 1px 3px rgba(0,0,0,0.4);"></div>',
        anchor: new naver.maps.Point(size/2, size/2),
      }},
      zIndex: sel ? 1000 : 100,
    }});

    const info = new naver.maps.InfoWindow({{
      content: '<div class="info-card">' + m.html + '</div>',
      borderWidth: 0, anchorSize: new naver.maps.Size(0, 0),
      pixelOffset: new naver.maps.Point(0, -size/2 - 4),
    }});
    naver.maps.Event.addListener(marker, 'mouseover',
      () => info.open(map, marker));
    naver.maps.Event.addListener(marker, 'mouseout',
      () => info.close());
  }});
}}
</script>
</body>
</html>"""


# =====================================================================
#  캐시
# =====================================================================
@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_resource
def get_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)


@st.cache_data
def get_max_ymd():
    return get_conn().execute("SELECT MAX(deal_ymd) FROM trades").fetchone()[0]


# =====================================================================
#  검색 파이프라인
# =====================================================================
def search_pipeline(query: str, include_road_jimok: bool):
    conn = get_conn()
    client = get_client()
    cond = parse_query(client, query)

    # emd_list 정규화 (단일 'emd' 키도 호환)
    emds = cond.get("emd_list") or (
        [cond["emd"]] if cond.get("emd") else []
    )
    allowed_emds = [e for e in emds if e in ALLOWED_EMD]
    out_of_range = bool(emds) and not allowed_emds
    cond["emd_list"] = allowed_emds  # 정규화된 리스트로 덮어씀

    matched_road = None
    road_info = None
    road_lines = None
    if cond.get("road_query"):
        rq = cond["road_query"]
        direct = conn.execute(
            "SELECT 1 FROM roads WHERE road_name = ? LIMIT 1", (rq,)
        ).fetchone()
        if direct:
            matched_road = rq
            road_info = {"matched": rq, "confidence": "exact",
                         "reason": "DB에 직접 일치"}
        else:
            cands = [r[0] for r in conn.execute(
                "SELECT DISTINCT road_name FROM roads "
                "WHERE rd_rank_h IN ('지방도', '국가지원지방도', '일반국도') "
                "AND road_name NOT IN ('', '-') ORDER BY road_name"
            )]
            road_info = map_road(client, rq, cands)
            matched_road = road_info.get("matched")

    where = ["resolved_pnu IS NOT NULL"]
    params = []
    if allowed_emds:
        where.append(
            "(" + " OR ".join("umd_name LIKE ?" for _ in allowed_emds) + ")"
        )
        params.extend(e + "%" for e in allowed_emds)
    else:
        # emd 명시 없으면 백암면+원삼면 둘 다 검색
        where.append("(umd_name LIKE '백암면%' OR umd_name LIKE '원삼면%')")
    if not include_road_jimok:
        where.append("jimok != '도로'")

    start_ymd, end_ymd = build_period_range(cond, get_max_ymd())
    if start_ymd:
        where.append("deal_ymd BETWEEN ? AND ?")
        params.extend([start_ymd, end_ymd])

    if cond.get("jimok_list"):
        jms = cond["jimok_list"]
        where.append(f"jimok IN ({','.join('?' * len(jms))})")
        params += jms
    if cond.get("exclude_jimok_list"):
        ex = cond["exclude_jimok_list"]
        where.append(f"jimok NOT IN ({','.join('?' * len(ex))})")
        params += ex
    for fld, col, op in [
        ("min_area_m2", "area_m2", ">="), ("max_area_m2", "area_m2", "<="),
        ("min_deal_amount", "deal_amount", ">="),
        ("max_deal_amount", "deal_amount", "<="),
        ("min_unit_per_pyeong", "unit_per_pyeong", ">="),
        ("max_unit_per_pyeong", "unit_per_pyeong", "<="),
    ]:
        if cond.get(fld) is not None:
            where.append(f"{col} {op} ?")
            params.append(cond[fld])
    if matched_road and cond.get("radius_m"):
        b = conn.execute(
            "SELECT MIN(min_lon), MAX(max_lon), MIN(min_lat), MAX(max_lat) "
            "FROM roads WHERE road_name = ?", (matched_road,)
        ).fetchone()
        if b and b[0] is not None:
            rad_deg = cond["radius_m"] / 111049.0
            where += ["resolved_lon BETWEEN ? AND ?",
                      "resolved_lat BETWEEN ? AND ?"]
            params += [b[0] - rad_deg, b[1] + rad_deg,
                       b[2] - rad_deg, b[3] + rad_deg]
            road_lines = [json.loads(r[0]) for r in conn.execute(
                "SELECT geometry_json FROM roads WHERE road_name = ?",
                (matched_road,))]

    sort_by = cond.get("sort_by") or "deal_ymd"
    sort_order = (cond.get("sort_order") or "desc").upper()
    if sort_by not in ("deal_ymd", "unit_per_pyeong", "area_m2", "deal_amount"):
        sort_by = "deal_ymd"
    if sort_order not in ("ASC", "DESC"):
        sort_order = "DESC"

    sql = (
        "SELECT id, umd_name, jimok, area_m2, deal_amount, deal_ymd, "
        "resolved_pnu, resolved_jibun, resolved_lon, resolved_lat, "
        "unit_per_pyeong, match_confidence, jibun_masked "
        "FROM trades WHERE " + " AND ".join(where) +
        f" ORDER BY {sort_by} {sort_order}"
    )
    bbox = list(conn.execute(sql, params))
    if road_lines:
        radius_m = cond["radius_m"]
        results = []
        for r in bbox:
            d = min(point_to_line_m(r["resolved_lon"], r["resolved_lat"], line)
                    for line in road_lines)
            if d <= radius_m:
                results.append((d, dict(r)))
    else:
        results = [(None, dict(r)) for r in bbox]
    if cond.get("exclude_shared"):
        pnu_count = Counter(r["resolved_pnu"] for _, r in results)
        results = [(d, r) for d, r in results if pnu_count[r["resolved_pnu"]] == 1]

    return {
        "cond": cond,
        "matched_road": matched_road,
        "road_info": road_info,
        "road_lines": road_lines,
        "start_ymd": start_ymd,
        "end_ymd": end_ymd,
        "results": results,
        "out_of_range": out_of_range,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }


# =====================================================================
#  Streamlit UI
# =====================================================================
st.set_page_config(
    page_title="용인 토지 실거래 검색",
    page_icon="🏞️", layout="wide",
)

with st.sidebar:
    st.title("🏞️ 토지 실거래")
    st.caption("자연어로 묻고 결과를 즉시")
    st.divider()
    st.subheader("ℹ️ 테스트 범위")
    st.info("**용인 처인구 백암면 · 원삼면**\n\n"
            "5년치 (2021-01 ~ 2025-12)\n약 6,766건")
    st.divider()
    st.subheader("⚙️ 표시 설정")
    include_road = st.toggle("도로 지목 포함", value=False,
                              help="국가·공공 수용 거래가 많아 기본 제외")
    st.divider()
    st.subheader("⚠️ 한계")
    st.caption(
        "• **'대'**(대지) 거래는 토지+건물 합산일 수 있어 시세 왜곡 가능\n\n"
        "• 별표 지번 복원 못 한 거래는 결과에서 제외\n\n"
        "• 본 도구는 참고용. 실거래 시 등기부등본·현장 확인 필수"
    )

    with st.expander("🐛 디버그 (동기화 확인용)"):
        st.caption(f"selected_pnu: `{st.session_state.get('selected_pnu')}`")
        st.caption(f"last_map_click_sig: `{st.session_state.get('last_map_click_sig')}`")
        st.caption(f"last_table_rows: `{st.session_state.get('last_table_rows')}`")

st.title("🔍 자연어 토지 실거래 검색")
st.caption(
    '예시: "원삼면 임야 평당 100 미만 최근 1년" · '
    '"덕평로 반경 3km 임야 1억 이하 면적 큰 순" · '
    '"백암면 2024년 상반기 전·답 단독매매만"'
)

query = st.text_input(
    "질의", placeholder="자연어로 원하는 조건을 한 줄 입력하세요",
    label_visibility="collapsed",
)
go = st.button("🔍 검색", type="primary", use_container_width=True)

if go and query:
    with st.spinner("자연어 분석 + 검색 중... (5초 정도)"):
        try:
            result = search_pipeline(query, include_road_jimok=include_road)
        except Exception as e:
            st.error(f"검색 오류: {type(e).__name__}: {e}")
            st.stop()
    st.session_state.result = result
    st.session_state.selected_pnu = None
    st.session_state.last_map_click_sig = None
    st.session_state.last_table_rows = []
    # 검색이 새로 일어났을 때만 지도 key를 갱신 (그 외에는 동일 key 유지)
    st.session_state.map_key = f"map_{datetime.now().timestamp()}"

# 결과 표시
if "result" in st.session_state:
    result = st.session_state.result
    cond = result["cond"]
    results = result["results"]

    if result["out_of_range"]:
        st.warning("⚠️ 테스트 버전은 **백암면 · 원삼면** 만 검색 가능. "
                   "그 외 지역은 무시.")

    with st.expander("🔧 자연어 파싱 결과 (확인용)"):
        cleaned = {k: v for k, v in cond.items()
                   if v is not None and v != [] and v != ""}
        st.json(cleaned)
        if result["matched_road"]:
            ri = result["road_info"] or {}
            st.write(
                f"**도로 매핑**: `{cond.get('road_query')}` → "
                f"**{result['matched_road']}**  ({ri.get('confidence')})"
            )
            if ri.get("reason"):
                st.caption(ri["reason"])
        if result["start_ymd"]:
            st.write(f"**기간**: {result['start_ymd']} ~ {result['end_ymd']}")

    if not results:
        st.warning("조건에 맞는 거래가 없어요. 다른 표현으로 시도해보세요.")
        st.stop()

    # 시세 요약
    st.subheader("💰 시세 요약")
    high = [(d, r) for d, r in results if r["match_confidence"] == "high"]
    pnu_count = Counter(r["resolved_pnu"] for _, r in high)
    solo = [(d, r) for d, r in high if pnu_count[r["resolved_pnu"]] == 1]
    units = [r["unit_per_pyeong"] for _, r in solo if r["unit_per_pyeong"]]
    cols = st.columns(4)
    cols[0].metric("전체 거래", f"{len(results):,}건")
    cols[1].metric("정상 시세 표본", f"{len(solo)}건",
                    help="확정 매칭 + 단독매매 (공유지분 거래 제외)")
    if units:
        med = statistics.median(units)
        avg = sum(units) / len(units)
        cols[2].metric("평단가 중앙값", f"{med:,.0f} 만원/평")
        cols[3].metric("평단가 평균", f"{avg:,.0f} 만원/평")
        prices = [r["deal_amount"] for _, r in solo]
        st.caption(
            f"평단가 범위 {min(units):,.0f} ~ {max(units):,.0f} 만원/평  ·  "
            f"거래금액 중앙값 **{statistics.median(prices):,.0f}만원**  ·  "
            f"정렬: `{result['sort_by']} {result['sort_order']}`"
        )
    else:
        cols[2].metric("평단가 중앙값", "—")
        cols[3].metric("평단가 평균", "—")

    st.divider()

    # 이전 rerun에서 결정된 selected_pnu (지도·표 그리기에 사용)
    prev_selected_pnu = st.session_state.get("selected_pnu")

    col_map, col_table = st.columns([1, 1])

    # ===== 지도 (V-World 베이스맵) =====
    with col_map:
        st.subheader("📍 거래 위치 (V-World 베이스맵)")

        # PNU별 거래 그룹 + 폴리곤 일괄 조회
        max_pins = 300
        pnu_groups = {}
        for d, r in results[:max_pins]:
            pnu = r.get("resolved_pnu")
            if not pnu:
                continue
            pnu_groups.setdefault(pnu, []).append((d, r))
        if prev_selected_pnu and prev_selected_pnu not in pnu_groups:
            for d, r in results[:500]:
                if r["resolved_pnu"] == prev_selected_pnu:
                    pnu_groups[prev_selected_pnu] = [(d, r)]
                    break

        conn_map = get_conn()
        geom_map = {}
        if pnu_groups:
            ph = ",".join("?" * len(pnu_groups))
            for row in conn_map.execute(
                f"SELECT pnu, geometry_json FROM parcels WHERE pnu IN ({ph})",
                list(pnu_groups.keys()),
            ):
                if row[1]:
                    geom_map[row[0]] = json.loads(row[1])

        # 중심·줌
        if prev_selected_pnu and prev_selected_pnu in pnu_groups:
            sel_r = pnu_groups[prev_selected_pnu][0][1]
            center = [sel_r["resolved_lat"], sel_r["resolved_lon"]]
            zoom = 17
        else:
            lons = [r["resolved_lon"] for _, r in results if r["resolved_lon"]]
            lats = [r["resolved_lat"] for _, r in results if r["resolved_lat"]]
            center = ([sum(lats) / len(lats), sum(lons) / len(lons)]
                      if lats and lons else [37.15, 127.35])
            zoom = 12

        def build_html(group):
            d, r = group[0]
            jb = r["resolved_jibun"] or "?"
            pyeong = (r["area_m2"] * PYEONG_PER_M2) if r["area_m2"] else 0
            lines = [
                f"<b>{r['umd_name']} {jb}</b>",
                f"<br>{r['jimok']} · {r['area_m2']:,.0f}㎡ "
                f"({pyeong:,.0f}평)",
            ]
            if len(group) == 1:
                lines.append(f"<br><b>{r['deal_amount']:,}만원</b>")
                if r["unit_per_pyeong"]:
                    lines.append(f" ({r['unit_per_pyeong']:,.0f}만원/평)")
                lines.append(
                    f"<br>{r['deal_ymd'][:10]} · 매칭 {r['match_confidence']}"
                )
            else:
                lines.append(f"<br><b>이 필지 거래 {len(group)}건</b>")
                for _, rg in group[:5]:
                    unit = (f" ({rg['unit_per_pyeong']:,.0f}만원/평)"
                            if rg["unit_per_pyeong"] else "")
                    lines.append(
                        f"<br>· {rg['deal_ymd'][:10]}  "
                        f"{rg['deal_amount']:,}만원{unit}"
                    )
                if len(group) > 5:
                    lines.append(f"<br>· ... 외 {len(group)-5}건")
            return "".join(lines)

        # 네이버 지도용 데이터 직렬화
        markers_data = []
        polygons_data = []
        for pnu, group in pnu_groups.items():
            d, r = group[0]
            is_sel = (pnu == prev_selected_pnu)
            jc = jimok_color(r["jimok"])
            html = build_html(group)
            if r["resolved_lon"] and r["resolved_lat"]:
                markers_data.append({
                    "pnu": pnu,
                    "lat": r["resolved_lat"],
                    "lon": r["resolved_lon"],
                    "color": jc,
                    "is_selected": is_sel,
                    "html": html,
                })
            geom = geom_map.get(pnu)
            if geom:
                coords = geom["coordinates"]
                if geom["type"] == "Polygon":
                    rings = [coords[0]]
                else:  # MultiPolygon
                    rings = [poly[0] for poly in coords]
                polygons_data.append({
                    "pnu": pnu, "coords": rings,
                    "color": jc, "is_selected": is_sel, "html": html,
                })

        # folium + V-World 베이스맵
        m = folium.Map(
            location=center, zoom_start=zoom,
            tiles=VWORLD_TILES, attr=VWORLD_ATTR,
        )

        # 도로 라인
        if result["road_lines"]:
            for line in result["road_lines"]:
                folium.PolyLine(
                    [[c[1], c[0]] for c in line],
                    color="blue", weight=3, opacity=0.5,
                ).add_to(m)

        # 그릴 순서: 선택된 PNU는 마지막에 (z-index 위에)
        ordered_pnus = [p for p in pnu_groups if p != prev_selected_pnu]
        if prev_selected_pnu and prev_selected_pnu in pnu_groups:
            ordered_pnus.append(prev_selected_pnu)

        # 1) 폴리곤 (평소 투명, 호버 시 지목 색)
        for pnu in ordered_pnus:
            group = pnu_groups[pnu]
            geom = geom_map.get(pnu)
            if not geom:
                continue
            r = group[0][1]
            is_sel = (pnu == prev_selected_pnu)
            jc = jimok_color(r["jimok"])
            html = build_html(group)
            if is_sel:
                style = {
                    "color": SELECTED_COLOR, "fillColor": SELECTED_COLOR,
                    "fillOpacity": 0.5, "weight": 3,
                }
            else:
                style = {
                    "color": "#ffffff", "fillColor": "#ffffff",
                    "fillOpacity": 0.0, "weight": 0.5,
                }
            highlight = {
                "color": jc, "fillColor": jc,
                "fillOpacity": 0.45, "weight": 3,
            }
            folium.GeoJson(
                {"type": "Feature", "geometry": geom, "properties": {}},
                style_function=(lambda x, s=style: s),
                highlight_function=(lambda x, h=highlight: h),
                tooltip=folium.Tooltip(html, sticky=False),
                popup=folium.Popup(html, max_width=320),
            ).add_to(m)

        # 2) 마커 (지목별 색)
        for pnu in ordered_pnus:
            group = pnu_groups[pnu]
            d, r = group[0]
            if r["resolved_lon"] is None or r["resolved_lat"] is None:
                continue
            is_sel = (pnu == prev_selected_pnu)
            jc = jimok_color(r["jimok"])
            marker_color = SELECTED_COLOR if is_sel else jc
            radius = 9 if is_sel else 6
            html = build_html(group)
            folium.CircleMarker(
                location=[r["resolved_lat"], r["resolved_lon"]],
                radius=radius, color=marker_color, fill=True,
                fill_color=marker_color,
                fill_opacity=0.9, weight=2,
                tooltip=folium.Tooltip(html, sticky=False),
                popup=folium.Popup(html, max_width=320),
            ).add_to(m)

        if len(results) > max_pins:
            st.caption(
                f"※ 지도에 최대 {max_pins}필지까지 표시 "
                f"(전체 {len(results)}건)"
            )

        # 지목 색 범례
        present_jimoks = sorted({r["jimok"] for _, r in results[:max_pins]
                                 if r["jimok"]})
        if present_jimoks:
            legend = "  ".join(
                f"<span style='color:{jimok_color(j)};font-weight:bold'>●</span> "
                f"{j}"
                for j in present_jimoks
            )
            st.markdown(legend, unsafe_allow_html=True)
        st.caption(
            "🔴 선택된 필지  ·  호버하면 지목 색으로 강조  ·  "
            "마커/필지 클릭하면 표에서 강조"
        )

        map_output = st_folium(
            m, center=center, zoom=zoom,
            width=None, height=520,
            returned_objects=["last_object_clicked"],
            key=st.session_state.get("map_key", "map_init"),
        )

    # 지도 클릭 → 가장 가까운 거래 PNU
    new_pnu_from_map = None
    if map_output and map_output.get("last_object_clicked"):
        clk = map_output["last_object_clicked"]
        sig = (round(clk["lat"], 7), round(clk["lng"], 7))
        if st.session_state.get("last_map_click_sig") != sig:
            best_pnu, best_dist2 = None, float("inf")
            for d, r in results[:500]:
                if r["resolved_lat"] is None:
                    continue
                d2 = ((r["resolved_lat"] - clk["lat"]) ** 2 +
                      (r["resolved_lon"] - clk["lng"]) ** 2)
                if d2 < best_dist2:
                    best_dist2 = d2
                    best_pnu = r["resolved_pnu"]
            if best_pnu:
                new_pnu_from_map = best_pnu
                st.session_state.last_map_click_sig = sig

    # ===== 표 =====
    with col_table:
        st.subheader("📋 거래 목록")
        df_rows = []
        for d, r in results:
            row = {}
            if result["road_lines"]:
                row["거리(m)"] = int(d) if d is not None else None
            row["동·리"] = r["umd_name"]
            row["지번"] = r["resolved_jibun"] or "?"
            row["지목"] = r["jimok"]
            row["면적(㎡)"] = int(r["area_m2"]) if r["area_m2"] else None
            row["면적(평)"] = (int(r["area_m2"] * PYEONG_PER_M2)
                                if r["area_m2"] else None)
            row["금액(만원)"] = r["deal_amount"]
            row["평단가(만원/평)"] = (int(r["unit_per_pyeong"])
                                        if r["unit_per_pyeong"] else None)
            row["시기"] = r["deal_ymd"][:10] if r["deal_ymd"] else ""
            row["PNU"] = r["resolved_pnu"] or ""
            row["신뢰도"] = r["match_confidence"]
            df_rows.append(row)
        df = pd.DataFrame(df_rows)

        # 엑셀 다운로드
        excel_buf = io.BytesIO()
        with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="거래목록", index=False)
        st.download_button(
            label=f"📥 엑셀로 내보내기 (전체 {len(df):,}건)",
            data=excel_buf.getvalue(),
            file_name=(
                f"land_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            ),
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            use_container_width=True,
        )
        st.caption(
            "💡 헤더 클릭으로 정렬 (오름차 ↔ 내림차)  ·  "
            "행 클릭 시 지도에서 그 필지가 확대·강조됨"
        )

        df_display = df.head(500).copy().reset_index(drop=True)

        # 선택된 PNU가 있고 5번째 행 아래에 있다면 5번째 위치로 이동
        # (정렬: [그 위 4건] + [선택 행] + [표시 안 됐던 위쪽] + [나머지 아래])
        if prev_selected_pnu:
            matches = df_display.index[
                df_display["PNU"] == prev_selected_pnu
            ].tolist()
            if matches:
                sel_idx = matches[0]
                if sel_idx > 4:
                    new_order = (
                        list(range(sel_idx - 4, sel_idx + 1))
                        + list(range(0, sel_idx - 4))
                        + list(range(sel_idx + 1, len(df_display)))
                    )
                    df_display = (
                        df_display.iloc[new_order].reset_index(drop=True)
                    )

        # selected_pnu가 있을 때 그 행 노란 배경
        def highlight_selected(row):
            if prev_selected_pnu and row.get("PNU") == prev_selected_pnu:
                return ["background-color: #fef08a; font-weight: bold"] * len(row)
            return [""] * len(row)

        styled = df_display.style.apply(highlight_selected, axis=1)

        event = st.dataframe(
            styled,
            use_container_width=True,
            height=450,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
        )
        if len(results) > 500:
            st.caption(
                f"※ 표에는 최대 500건만. 전체 {len(results):,}건은 엑셀로."
            )

    # 표 selection 변경 감지 (사용자가 새로 클릭한 건지, 이전 selection 유지인지)
    try:
        sel_rows = list(event.selection.rows)
    except AttributeError:
        try:
            sel_rows = list(event["selection"]["rows"])
        except (KeyError, TypeError):
            sel_rows = []

    prev_table_rows = st.session_state.get("last_table_rows", [])
    table_changed = sel_rows != prev_table_rows
    new_pnu_from_table = None
    if table_changed:
        st.session_state.last_table_rows = sel_rows
        if sel_rows:
            try:
                v = df_display.iloc[sel_rows[0]]["PNU"]
                if v:
                    new_pnu_from_table = v
            except (IndexError, KeyError):
                pass

    # 우선순위: 새 지도 클릭 → 새 표 클릭 → 이전 유지
    if new_pnu_from_map is not None:
        new_sel = new_pnu_from_map
    elif table_changed:
        new_sel = new_pnu_from_table  # None이면 선택 해제됨
    else:
        new_sel = prev_selected_pnu

    if new_sel != prev_selected_pnu:
        st.session_state.selected_pnu = new_sel
        st.rerun()
