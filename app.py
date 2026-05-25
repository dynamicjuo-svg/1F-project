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

# 네이버 지도 양방향 컴포넌트 (declare_component) — 클릭 시 setComponentValue로 PNU 반환
_NAVER_MAP_COMPONENT = components.declare_component(
    "of_naver_map",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "naver_map_component"),
)

# 거래 표 양방향 컴포넌트 — 헤더 클릭 정렬 보존 + 행 클릭 양방향
_TRADES_TABLE_COMPONENT = components.declare_component(
    "of_trades_table",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "trades_table_component"),
)


def of_trades_table(rows, columns, selected_pnu=None,
                     initial_sort_col=None, initial_sort_dir="desc", key=None):
    """헤더 클릭 정렬 보존 + 행 클릭 양방향 표 컴포넌트."""
    return _TRADES_TABLE_COMPONENT(
        rows=rows or [],
        columns=columns or [],
        selected_pnu=selected_pnu,
        initial_sort_col=initial_sort_col,
        initial_sort_dir=initial_sort_dir,
        default=None,
        key=key,
    )


def of_naver_map(client_id, center, zoom, markers, polygons,
                  road_lines=None, sel_color="#1e40af",
                  zoom_label_threshold=15, recenter=False, key=None):
    """네이버 지도 양방향 컴포넌트 호출. 폴리곤/마커 클릭 시 dict 반환."""
    return _NAVER_MAP_COMPONENT(
        client_id=client_id,
        center=list(center),
        zoom=int(zoom),
        markers=markers or [],
        polygons=polygons or [],
        road_lines=road_lines or [],
        sel_color=sel_color,
        zoom_label_threshold=int(zoom_label_threshold),
        recenter=bool(recenter),
        default=None,
        key=key,
    )
from branca.element import MacroElement
from jinja2 import Template
from streamlit_folium import st_folium

from api_keys import ANTHROPIC_KEY, VWORLD_KEY, NAVER_MAP_CLIENT_ID
from search import (parse_query, map_road, point_to_line_m, build_period_range,
                    lookup_reference_parcel, fill_cond_from_reference,
                    parse_listing, find_parcel_candidates)


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
STATIC_DIR = os.path.join(HERE, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
def _load_allowed_emd():
    """region_prefix_cache.json에서 모든 emd 추출. 처인구(+추후 기흥/수지) 전체."""
    import json as _json
    p = os.path.join(HERE, "region_prefix_cache.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            d = _json.load(f)
        emds = set(d.get("emd_map", {}).keys())
        if emds:
            return emds
    return {"백암면", "원삼면"}  # fallback


ALLOWED_EMD = _load_allowed_emd()
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


# 마커 라벨을 줌 임계값 이상에서만 표시
# - CSS: 처음에 .zoom-label 숨김 (JS 실패해도 안전)
# - JS: setInterval로 지속 모니터링하며 토글
class ZoomLabelToggle(MacroElement):
    _template = Template("""
        {% macro header(this, kwargs) %}
        <style>
            .leaflet-tooltip.zoom-label {
                display: none;
                background: rgba(255,255,255,0.92);
                border: 1px solid #d4d4d8;
                border-radius: 3px;
                font-size: 11px;
                padding: 1px 5px;
                box-shadow: 0 1px 2px rgba(0,0,0,0.15);
            }
            .leaflet-tooltip.zoom-label.zoom-label-show {
                display: block;
            }
        </style>
        {% endmacro %}

        {% macro script(this, kwargs) %}
        (function() {
            var threshold = {{this.threshold}};
            function findMap() {
                for (var k in window) {
                    try {
                        var v = window[k];
                        if (v && typeof v.getZoom === 'function') {
                            return v;
                        }
                    } catch (e) {}
                }
                return null;
            }
            function apply() {
                var mapObj = findMap();
                if (!mapObj) return;
                var show = mapObj.getZoom() >= threshold;
                document.querySelectorAll('.zoom-label').forEach(function(el) {
                    if (show) el.classList.add('zoom-label-show');
                    else el.classList.remove('zoom-label-show');
                });
            }
            // 300ms마다 — 새 마커 추가·줌 변경 모두 잡힘
            setInterval(apply, 300);
            setTimeout(apply, 100);
        })();
        {% endmacro %}
    """)

    def __init__(self, threshold=15):
        super().__init__()
        self._name = "ZoomLabelToggle"
        self.threshold = threshold


# =====================================================================
#  네이버 지도 HTML 빌더 (streamlit components iframe 임베드)
# =====================================================================
def build_naver_map_html(client_id, center, zoom, markers, polygons,
                          road_lines, height=520, zoom_label_threshold=15):
    """네이버 지도 v3 SDK + 줌 임계값 기반 라벨 토글."""
    markers_json = json.dumps(markers, ensure_ascii=False)
    polygons_json = json.dumps(polygons, ensure_ascii=False)
    roads_json = json.dumps(road_lines or [], ensure_ascii=False)
    sel_color = SELECTED_COLOR

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script src="https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId={client_id}"></script>
<style>
  body, html {{ margin: 0; padding: 0; font-family: system-ui, sans-serif; }}
  #map {{ width: 100%; height: {height}px; }}
  .info-card {{
    padding: 10px 12px; font-size: 12px; line-height: 1.5;
    max-width: 320px; background: white;
    border: 1px solid #d4d4d8; border-radius: 6px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
  }}
  .marker-wrap {{ display: flex; align-items: center; cursor: pointer; }}
  .marker-dot {{
    border-radius: 50%; border: 2px solid white;
    box-shadow: 0 1px 3px rgba(0,0,0,0.4);
  }}
  .marker-label {{
    margin-left: 6px;
    background: rgba(255,255,255,0.92);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px; font-weight: 500;
    color: #1f2937; white-space: nowrap;
    box-shadow: 0 1px 2px rgba(0,0,0,0.15);
    display: none;
  }}
  .marker-label.show {{ display: inline-block; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
// 인증 실패 시 명확한 메시지 출력
window.navermap_authFailure = function() {{
  document.getElementById('map').innerHTML =
    '<div style="padding:24px;color:#dc2626;font-size:13px;line-height:1.6;">'
    + '<b>네이버 지도 인증 실패</b><br>'
    + 'NCP 콘솔의 서비스 URL에 현재 도메인이 등록돼 있는지 확인하세요.<br>'
    + 'Client ID: <code>{client_id}</code></div>';
}};

if (typeof naver === 'undefined' || !naver.maps) {{
  document.getElementById('map').innerHTML =
    '<div style="padding:24px;color:#dc2626;font-size:13px;">'
    + '네이버 지도 SDK 로드 실패. 키 또는 네트워크 확인 필요.</div>';
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

  // 지적편집도 레이어 (네이버 정책: 줌 14 이상에서만 격자 렌더링)
  const cadastralLayer = new naver.maps.CadastralLayer();
  cadastralLayer.setMap(map);
  const CADASTRAL_MIN_ZOOM = 14;

  // 토글 + 현재 줌 라이브 표시 (모바일 디버깅 + 안내)
  const cadBtn = document.createElement('button');
  cadBtn.style.cssText =
    'position:absolute;top:10px;left:10px;z-index:1000;' +
    'padding:10px 14px;background:white;border:1px solid #c0c0c0;' +
    'border-radius:6px;cursor:pointer;font-size:13px;' +
    'box-shadow:0 1px 3px rgba(0,0,0,0.2);font-weight:500;' +
    'touch-action:manipulation;-webkit-tap-highlight-color:transparent;';

  // 별도 안내 배너 (줌 부족 시)
  const cadHint = document.createElement('div');
  cadHint.style.cssText =
    'position:absolute;top:60px;left:10px;z-index:1000;' +
    'padding:6px 10px;background:#fef3c7;color:#92400e;' +
    'border:1px solid #fbbf24;border-radius:4px;font-size:11px;' +
    'box-shadow:0 1px 3px rgba(0,0,0,0.15);display:none;';
  cadHint.innerText = '🔍 핀치줌으로 더 확대 → 줌 14↑';

  function refreshCadBtn() {{
    const z = map.getZoom();
    const on = !!cadastralLayer.getMap();
    const enough = z >= CADASTRAL_MIN_ZOOM;
    cadBtn.innerText = '🗺️ 지번 ' + (on ? 'ON' : 'OFF') + ' · zoom ' + z;
    cadBtn.style.opacity = on ? '1' : '0.6';
    cadBtn.style.background = (on && !enough) ? '#fff7ed' : 'white';
    cadHint.style.display = (on && !enough) ? 'block' : 'none';
  }}
  cadBtn.onclick = function() {{
    if (cadastralLayer.getMap()) {{
      cadastralLayer.setMap(null);
    }} else {{
      cadastralLayer.setMap(map);
    }}
    refreshCadBtn();
  }};
  document.getElementById('map').appendChild(cadBtn);
  document.getElementById('map').appendChild(cadHint);

  const ZOOM_THRESHOLD = {zoom_label_threshold};
  function updateLabels() {{
    const show = map.getZoom() >= ZOOM_THRESHOLD;
    document.querySelectorAll('.marker-label').forEach(function(el) {{
      if (show) el.classList.add('show');
      else el.classList.remove('show');
    }});
  }}
  naver.maps.Event.addListener(map, 'zoom_changed', function() {{
    updateLabels();
    refreshCadBtn();
  }});
  refreshCadBtn();

  // 도로 라인
  const roads = {roads_json};
  roads.forEach(line => {{
    const path = line.map(c => new naver.maps.LatLng(c[1], c[0]));
    new naver.maps.Polyline({{
      map: map, path: path,
      strokeColor: '#3b82f6', strokeWeight: 3, strokeOpacity: 0.6,
    }});
  }});

  // 폴리곤 — 검색 결과 필지 (모바일 호환: 디폴트로도 보이게)
  const SEL = '{sel_color}';
  const polygons = {polygons_json};
  // 모바일에서 호버 없으니 디폴트 strokeOpacity/Weight 강화
  const IS_TOUCH = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0);
  const DEFAULT_FILL_OPACITY = IS_TOUCH ? 0.12 : 0.0;
  const DEFAULT_STROKE_OPACITY = IS_TOUCH ? 0.85 : 0.4;
  const DEFAULT_STROKE_WEIGHT = IS_TOUCH ? 2.0 : 0.8;
  polygons.forEach(p => {{
    const paths = p.coords.map(ring =>
      ring.map(c => new naver.maps.LatLng(c[1], c[0]))
    );
    const isSel = p.is_selected;
    const polygon = new naver.maps.Polygon({{
      map: map, paths: paths,
      fillColor: isSel ? SEL : p.color,
      fillOpacity: isSel ? 0.45 : DEFAULT_FILL_OPACITY,
      strokeColor: isSel ? SEL : p.color,
      strokeOpacity: isSel ? 1 : DEFAULT_STROKE_OPACITY,
      strokeWeight: isSel ? 3 : DEFAULT_STROKE_WEIGHT,
      clickable: true,
    }});
    const info = new naver.maps.InfoWindow({{
      content: '<div class="info-card">' + p.html + '</div>',
      borderWidth: 0, anchorSize: new naver.maps.Size(0, 0),
      pixelOffset: new naver.maps.Point(0, -8),
    }});
    if (!isSel) {{
      const emphasize = (e) => {{
        polygon.setOptions({{
          fillOpacity: 0.45, strokeColor: p.color,
          strokeOpacity: 1, strokeWeight: 3,
        }});
        if (e && e.coord) info.open(map, e.coord);
      }};
      const dim = () => {{
        polygon.setOptions({{
          fillOpacity: DEFAULT_FILL_OPACITY, strokeColor: p.color,
          strokeOpacity: DEFAULT_STROKE_OPACITY,
          strokeWeight: DEFAULT_STROKE_WEIGHT,
        }});
        info.close();
      }};
      naver.maps.Event.addListener(polygon, 'mouseover', emphasize);
      naver.maps.Event.addListener(polygon, 'mouseout', dim);
      // 모바일/터치 탭 — 처음 탭 강조, 두 번째 탭 다시 옅게
      let on = false;
      naver.maps.Event.addListener(polygon, 'click', (e) => {{
        if (on) {{ dim(); on = false; }}
        else {{ emphasize(e); on = true; }}
      }});
    }} else {{
      naver.maps.Event.addListener(polygon, 'mouseover', e => info.open(map, e.coord));
      naver.maps.Event.addListener(polygon, 'mouseout', () => info.close());
      naver.maps.Event.addListener(polygon, 'click', e => info.open(map, e.coord));
    }}
  }});

  // 선택된 필지가 있으면 그 폴리곤 bounds에 자동 fit (필지 모양대로 줌인)
  const selPolygon = polygons.find(p => p.is_selected);
  if (selPolygon) {{
    const bounds = new naver.maps.LatLngBounds();
    selPolygon.coords.forEach(ring => {{
      ring.forEach(c => bounds.extend(new naver.maps.LatLng(c[1], c[0])));
    }});
    map.fitBounds(bounds, {{ top: 80, right: 60, bottom: 60, left: 60 }});
    // fitBounds 후 자동 마진. 너무 가까이 붙으면 한 단계만 빼서 시야 확보
    setTimeout(() => {{
      const z = map.getZoom();
      if (z > 20) map.setZoom(20);
    }}, 100);
  }}

  // 마커 — 지목별 색 + 줌 임계값 기반 라벨
  const markers = {markers_json};
  markers.forEach(m => {{
    const sel = m.is_selected;
    const size = sel ? 16 : 12;
    const color = sel ? SEL : m.color;
    const dotStyle = `background:${{color}};width:${{size}}px;height:${{size}}px;`;
    const labelText = (m.label || '').replace(/</g, '&lt;');
    const content =
      '<div class="marker-wrap">' +
      '<div class="marker-dot" style="' + dotStyle + '"></div>' +
      '<div class="marker-label">' + labelText + '</div>' +
      '</div>';
    const marker = new naver.maps.Marker({{
      position: new naver.maps.LatLng(m.lat, m.lon),
      map: map,
      icon: {{
        content: content,
        anchor: new naver.maps.Point(size/2, size/2),
      }},
      zIndex: sel ? 1000 : 100,
    }});
    const info = new naver.maps.InfoWindow({{
      content: '<div class="info-card">' + m.html + '</div>',
      borderWidth: 0, anchorSize: new naver.maps.Size(0, 0),
      pixelOffset: new naver.maps.Point(0, -size/2 - 4),
    }});
    naver.maps.Event.addListener(marker, 'mouseover', () => info.open(map, marker));
    naver.maps.Event.addListener(marker, 'mouseout', () => info.close());
  }});

  // 초기 라벨 표시 (마커 모두 추가된 후)
  setTimeout(updateLabels, 50);
  setTimeout(updateLabels, 400);
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

    # 참조 필지 (reference_jibun) — 비슷한 조건 자동 cond 채움
    reference_info = None
    if cond.get("reference_jibun"):
        ref = lookup_reference_parcel(conn, cond["reference_jibun"])
        if ref:
            notes = fill_cond_from_reference(cond, ref)
            reference_info = {"ref": ref, "notes": notes}

    # emd_list 정규화 (단일 'emd' 키도 호환)
    emds = cond.get("emd_list") or (
        [cond["emd"]] if cond.get("emd") else []
    )
    allowed_emds = [e for e in emds if e in ALLOWED_EMD]
    out_of_range = bool(emds) and not allowed_emds
    cond["emd_list"] = allowed_emds  # 정규화된 리스트로 덮어씀

    matched_roads = []  # 매칭된 도로명 리스트 (1개 또는 여러 개)
    road_info = None
    road_lines = None
    if cond.get("road_query"):
        rq = cond["road_query"]
        direct = conn.execute(
            "SELECT 1 FROM roads WHERE road_name = ? LIMIT 1", (rq,)
        ).fetchone()
        if direct:
            matched_roads = [rq]
            road_info = {"matched": rq, "confidence": "exact",
                         "reason": "DB에 직접 일치"}
        else:
            cands = [r[0] for r in conn.execute(
                "SELECT DISTINCT road_name FROM roads "
                "WHERE rd_rank_h IN ('지방도', '국가지원지방도', '일반국도') "
                "AND road_name NOT IN ('', '-') ORDER BY road_name"
            )]
            road_info = map_road(client, rq, cands)
            m = road_info.get("matched")
            if isinstance(m, list):
                matched_roads = m
            elif m:
                matched_roads = [m]
    # 단일 표시용 (UI 등 호환)
    matched_road = (matched_roads[0] if len(matched_roads) == 1
                    else (", ".join(matched_roads) if matched_roads else None))

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

    # 입지/규제 조건 (parcels 컬럼) — 서브쿼리로 합침
    parcels_conds = []
    parcels_params = []
    for fld, col, op in [
        ("min_elevation_m", "elevation_m", ">="),
        ("max_elevation_m", "elevation_m", "<="),
        ("max_slope_deg",   "slope_deg",   "<="),
        ("max_stream_dist_m", "dist_to_stream_m", "<="),
        ("min_stream_dist_m", "dist_to_stream_m", ">="),
    ]:
        if cond.get(fld) is not None:
            parcels_conds.append(f"{col} {op} ?")
            parcels_params.append(cond[fld])
    if cond.get("zone_include"):
        zs = cond["zone_include"]
        parcels_conds.append(f"zone_type IN ({','.join('?' * len(zs))})")
        parcels_params += zs
    if cond.get("zone_exclude"):
        zs = cond["zone_exclude"]
        parcels_conds.append(f"zone_type NOT IN ({','.join('?' * len(zs))})")
        parcels_params += zs
    if cond.get("exclude_gb"):
        parcels_conds.append("(is_gb IS NULL OR is_gb = 0)")
    if cond.get("exclude_protected_forest"):
        parcels_conds.append("(is_protected_forest IS NULL OR is_protected_forest = 0)")
    if cond.get("exclude_farm_promote"):
        parcels_conds.append("(is_farm_promote IS NULL OR is_farm_promote = 0)")
    if cond.get("require_road_access"):
        parcels_conds.append("has_road_access = 1")
    if cond.get("exclude_road_access"):
        parcels_conds.append("(has_road_access IS NULL OR has_road_access = 0)")
    # 신규: frontage / corner 필터
    if cond.get("min_road_frontage_m") is not None:
        parcels_conds.append("road_frontage_m >= ?")
        parcels_params.append(cond["min_road_frontage_m"])
    if cond.get("require_corner_lot"):
        parcels_conds.append("is_corner_lot = 1")
    if cond.get("exclude_flood"):
        parcels_conds.append("(flood_risk IS NULL OR flood_risk = 0)")
    if cond.get("shape_include"):
        ss = cond["shape_include"]
        parcels_conds.append(f"shape_type IN ({','.join('?' * len(ss))})")
        parcels_params += ss
    if cond.get("shape_exclude"):
        ss = cond["shape_exclude"]
        parcels_conds.append(f"shape_type NOT IN ({','.join('?' * len(ss))})")
        parcels_params += ss
    if parcels_conds:
        where.append(
            "resolved_pnu IN (SELECT pnu FROM parcels WHERE "
            + " AND ".join(parcels_conds) + ")"
        )
        params += parcels_params

    if matched_roads and cond.get("radius_m"):
        placeholders = ",".join("?" * len(matched_roads))
        b = conn.execute(
            f"SELECT MIN(min_lon), MAX(max_lon), MIN(min_lat), MAX(max_lat) "
            f"FROM roads WHERE road_name IN ({placeholders})",
            matched_roads,
        ).fetchone()
        if b and b[0] is not None:
            rad_deg = cond["radius_m"] / 111049.0
            where += ["resolved_lon BETWEEN ? AND ?",
                      "resolved_lat BETWEEN ? AND ?"]
            params += [b[0] - rad_deg, b[1] + rad_deg,
                       b[2] - rad_deg, b[3] + rad_deg]
            road_lines = [json.loads(r[0]) for r in conn.execute(
                f"SELECT geometry_json FROM roads "
                f"WHERE road_name IN ({placeholders})",
                matched_roads)]

    sort_by = cond.get("sort_by") or "deal_ymd"
    sort_order = (cond.get("sort_order") or "desc").upper()
    if sort_by not in ("deal_ymd", "unit_per_pyeong", "area_m2", "deal_amount"):
        sort_by = "deal_ymd"
    if sort_order not in ("ASC", "DESC"):
        sort_order = "DESC"

    sql = (
        "SELECT id, sigg_cd, umd_name, jimok, area_m2, deal_amount, deal_ymd, "
        "resolved_pnu, resolved_jibun, resolved_lon, resolved_lat, "
        "unit_per_pyeong, match_confidence, jibun_masked, "
        "price_anomaly, share_label "
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
    # 검색 결과 내 PNU 빈도 기반 공유지분 라벨링
    pnu_count = Counter(r["resolved_pnu"] for _, r in results)
    def _group_label(n):
        if n <= 1: return "단독"
        if n <= 3: return "공유지분"
        if n <= 7: return "다수공유"
        return "대규모공유"
    for _, r in results:
        r["share_group"] = _group_label(pnu_count[r["resolved_pnu"]])

    if cond.get("exclude_shared"):
        results = [(d, r) for d, r in results if r["share_group"] == "단독"]

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
        "reference_info": reference_info,
    }


# =====================================================================
#  GPT 스타일 응답 — 파싱된 cond를 자연어 문장으로 풀이
# =====================================================================
def format_cond_as_sentence(cond, result):
    """검색 의도를 자연어 문장 + 항목별 칩으로 정리."""
    parts = []  # 본문 문장 조각
    chips = []  # 시각적 태그용 (라벨, 값)

    # 지역
    emds = cond.get("emd_list") or ([cond["emd"]] if cond.get("emd") else [])
    if emds:
        parts.append(f"**{' · '.join(emds)}**")
        for e in emds:
            chips.append(("📍 지역", e))

    # 참조 필지 우선
    ref_info = result.get("reference_info") if isinstance(result, dict) else None
    if ref_info and ref_info.get("ref"):
        ref = ref_info["ref"]
        py = ref["area_m2"] / 3.3058 if ref.get("area_m2") else 0
        parts.append(
            f"**{ref['jibun']}**({ref['jimok']}, {py:,.0f}평)과 비슷한 조건"
        )
        chips.append(("🎯 참조 필지", f"{ref['jibun']} · {py:,.0f}평"))
        level = (cond.get("similarity_level") or "normal").lower()
        level_kr = {"strict": "엄격", "normal": "보통", "loose": "넉넉"}.get(level, level)
        chips.append(("⚖️ 유사도", level_kr))

    # 지목
    jms = cond.get("jimok_list") or []
    if jms:
        parts.append(f"지목 **{' · '.join(jms)}**")
        chips.append(("🏷️ 지목", " · ".join(jms)))
    ex_jms = cond.get("exclude_jimok_list") or []
    if ex_jms:
        chips.append(("🚫 지목 제외", " · ".join(ex_jms)))

    # 면적
    if cond.get("min_area_m2") and cond.get("max_area_m2"):
        mn_py = cond["min_area_m2"] / 3.3058
        mx_py = cond["max_area_m2"] / 3.3058
        chips.append(("📐 면적", f"{mn_py:,.0f}~{mx_py:,.0f}평"))
    elif cond.get("min_area_m2"):
        chips.append(("📐 면적", f"{cond['min_area_m2']/3.3058:,.0f}평↑"))
    elif cond.get("max_area_m2"):
        chips.append(("📐 면적", f"{cond['max_area_m2']/3.3058:,.0f}평↓"))

    # 금액 · 평단가
    if cond.get("min_deal_amount") or cond.get("max_deal_amount"):
        mn = cond.get("min_deal_amount") or 0
        mx = cond.get("max_deal_amount") or 0
        label = "💰 금액"
        if mn and mx:
            chips.append((label, f"{mn:,}~{mx:,}만원"))
        elif mn:
            chips.append((label, f"{mn:,}만원↑"))
        else:
            chips.append((label, f"{mx:,}만원↓"))
    if cond.get("min_unit_per_pyeong") or cond.get("max_unit_per_pyeong"):
        mn = cond.get("min_unit_per_pyeong") or 0
        mx = cond.get("max_unit_per_pyeong") or 0
        label = "💵 평단가"
        if mn and mx:
            chips.append((label, f"{mn}~{mx}만/평"))
        elif mn:
            chips.append((label, f"{mn}만/평↑"))
        else:
            chips.append((label, f"{mx}만/평↓"))

    # 기간
    start = result.get("start_ymd") if isinstance(result, dict) else None
    end = result.get("end_ymd") if isinstance(result, dict) else None
    if start and end:
        chips.append(("📅 기간", f"{start[:7]} ~ {end[:7]}"))

    # 도로
    if result and result.get("matched_road"):
        radius = cond.get("radius_m")
        rs = result["matched_road"]
        if "," in rs:
            rs = rs.split(",")[0] + f" 외 {len(rs.split(','))-1}"
        chips.append(("🛣️ 도로", f"{rs}" + (f" 반경 {radius/1000:g}km" if radius else "")))

    # 입지/규제
    if cond.get("min_elevation_m") is not None or cond.get("max_elevation_m") is not None:
        mn = cond.get("min_elevation_m")
        mx = cond.get("max_elevation_m")
        if mn is not None and mx is not None:
            chips.append(("⛰️ 해발", f"{mn:.0f}~{mx:.0f}m"))
        elif mn is not None:
            chips.append(("⛰️ 해발", f"{mn:.0f}m↑"))
        else:
            chips.append(("⛰️ 해발", f"{mx:.0f}m↓"))
    if cond.get("max_slope_deg") is not None:
        chips.append(("📉 경사", f"{cond['max_slope_deg']:g}°↓"))
    if cond.get("require_road_access"):
        chips.append(("🛤️ 도로 접면", "필수"))
    if cond.get("exclude_road_access"):
        chips.append(("🛤️ 도로 접면", "맹지만"))
    if cond.get("max_stream_dist_m"):
        chips.append(("🌊 하천", f"{cond['max_stream_dist_m']:g}m 이내"))
    if cond.get("zone_include"):
        chips.append(("🏛️ 용도지역", " · ".join(cond["zone_include"])))
    if cond.get("zone_exclude"):
        chips.append(("🏛️ 용도 제외", " · ".join(cond["zone_exclude"])))
    if cond.get("exclude_gb"):
        chips.append(("🚫", "그린벨트 제외"))
    if cond.get("exclude_protected_forest"):
        chips.append(("🚫", "보전산지 제외"))
    if cond.get("exclude_farm_promote"):
        chips.append(("🚫", "농업진흥구역 제외"))
    if cond.get("exclude_flood"):
        chips.append(("🚫", "침수예상 제외"))
    if cond.get("exclude_shared"):
        chips.append(("👤", "단독매매만"))

    # 정렬
    sort_by = cond.get("sort_by") or "deal_ymd"
    sort_order = (cond.get("sort_order") or "desc").lower()
    sort_label = {
        "deal_ymd": "최근순" if sort_order == "desc" else "오래된순",
        "unit_per_pyeong": "평단가 높은순" if sort_order == "desc" else "평단가 낮은순",
        "area_m2": "면적 큰순" if sort_order == "desc" else "면적 작은순",
        "deal_amount": "금액 높은순" if sort_order == "desc" else "금액 낮은순",
    }.get(sort_by, sort_by)
    chips.append(("⇅ 정렬", sort_label))

    # 본문 문장
    n_results = len(result.get("results", [])) if isinstance(result, dict) else 0
    headline = " · ".join(parts) if parts else "조건에 맞는 거래"
    sentence = f"{headline}의 실거래를 정리했습니다. 총 **{n_results:,}건** 매칭됐어요."

    return sentence, chips


# =====================================================================
#  Streamlit UI — OneFamily 실거래가 (Brutalist Bold 컨셉)
# =====================================================================
BRAND_NAVY = "#0a0a0a"        # 검정 (primary)
BRAND_NAVY_DEEP = "#0a0a0a"   # 검정 그대로
BRAND_NAVY_LIGHT = "#fef3c7"  # 노란 베이지 BG
BRAND_RED = "#dc2626"         # 강조 빨강
BRAND_YELLOW = "#fbbf24"      # 보조 노랑

APP_VERSION = "v2.1"
APP_RELEASE_DATE = "2026-05-25"

st.set_page_config(
    page_title="OneFamily 실거래가",
    page_icon="🟨", layout="wide",
    initial_sidebar_state="collapsed",  # 시안 4: 기본 접힘, 햄버거 아이콘만
)

# 글로벌 CSS — Brutalist Bold
st.markdown(f"""
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Pretendard:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: {BRAND_NAVY};
    --bg: {BRAND_NAVY_LIGHT};
    --red: {BRAND_RED};
    --yellow: {BRAND_YELLOW};
  }}
  /* 배경 — 노란 + 옅은 격자 */
  .stApp {{
    background-color: {BRAND_NAVY_LIGHT};
    background-image:
      linear-gradient(rgba(10,10,10,0.18) 1px, transparent 1px),
      linear-gradient(90deg, rgba(10,10,10,0.18) 1px, transparent 1px);
    background-size: 80px 80px;
    background-position: -1px -1px;
  }}
  .main .block-container {{
    padding-top: 2rem; padding-bottom: 4rem;
  }}
  /* 헤더 브랜드 */
  .of-brand {{
    display: flex; align-items: center; gap: 14px;
    margin-bottom: 6px;
  }}
  .of-brand .of-logo-box {{
    width: 48px; height: 48px; background: {BRAND_RED};
    border: 3px solid {BRAND_NAVY}; box-shadow: 4px 4px 0 {BRAND_NAVY};
    display: flex; align-items: center; justify-content: center;
    color: white; font-family: 'Archivo Black', sans-serif; font-size: 16px;
    flex-shrink: 0;
  }}
  .of-brand .of-logo {{
    font-family: 'Archivo Black', 'Pretendard', sans-serif;
    font-size: 26px; color: {BRAND_NAVY}; letter-spacing: -0.02em;
    line-height: 1.1;
  }}
  .of-brand .of-logo-accent {{ color: {BRAND_RED}; }}
  .of-brand .of-badge {{
    display: inline-block; background: {BRAND_NAVY}; color: {BRAND_NAVY_LIGHT};
    padding: 6px 12px; border: 3px solid {BRAND_NAVY};
    box-shadow: 3px 3px 0 {BRAND_RED};
    font-family: 'Archivo Black', sans-serif;
    font-size: 10px; letter-spacing: 0.06em;
  }}
  .of-brand-sub {{
    color: {BRAND_NAVY}; font-size: 14px; font-weight: 600;
    margin-top: 6px; margin-bottom: 16px;
  }}
  .of-version {{
    font-family: 'Archivo Black', monospace;
    font-size: 10px; color: #6a6a6a; letter-spacing: 0.08em;
    margin-bottom: 18px;
  }}
  /* GPT 응답 카드 — 검정 BG + 노란 텍스트 + 빨강 그림자 */
  .of-gpt-card {{
    background: {BRAND_NAVY}; color: {BRAND_NAVY_LIGHT};
    border: 3px solid {BRAND_NAVY};
    box-shadow: 8px 8px 0 {BRAND_RED};
    padding: 22px 26px; margin: 18px 0 26px 0;
    font-size: 15px; line-height: 1.6;
  }}
  .of-gpt-card .of-gpt-icon {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 30px; background: {BRAND_RED}; color: white;
    border: 2px solid {BRAND_NAVY_LIGHT};
    font-family: 'Archivo Black'; font-size: 12px;
    margin-right: 10px; vertical-align: middle;
  }}
  .of-gpt-card .of-gpt-title {{
    font-family: 'Archivo Black', sans-serif; color: {BRAND_YELLOW};
    font-size: 11px; letter-spacing: 0.1em;
    margin-bottom: 10px; display: flex; align-items: center;
  }}
  .of-gpt-card b {{ color: {BRAND_YELLOW}; font-weight: 800; }}
  /* 메트릭 카드 — Brutalist */
  div[data-testid="stMetric"] {{
    background: white; padding: 18px 20px;
    border: 3px solid {BRAND_NAVY}; box-shadow: 5px 5px 0 {BRAND_NAVY};
    border-radius: 0;
    transition: all 0.12s ease;
  }}
  div[data-testid="stMetric"]:hover {{
    transform: translate(-2px, -2px);
    box-shadow: 7px 7px 0 {BRAND_NAVY};
  }}
  div[data-testid="stMetric"] > div:first-child label {{
    font-family: 'Archivo Black', sans-serif !important;
    font-size: 10px !important; color: {BRAND_NAVY} !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase;
  }}
  div[data-testid="stMetric"] [data-testid="stMetricValue"] {{
    font-family: 'Archivo Black', 'Pretendard', sans-serif;
    color: {BRAND_NAVY}; font-weight: 900; font-size: 36px;
    letter-spacing: -0.03em;
  }}
  /* primary 버튼 */
  div.stButton > button[kind="primary"] {{
    background: {BRAND_NAVY}; color: white;
    border: 3px solid {BRAND_NAVY};
    box-shadow: 4px 4px 0 {BRAND_RED};
    font-family: 'Archivo Black', 'Pretendard', sans-serif;
    font-weight: 900; letter-spacing: 0.04em;
    border-radius: 0;
    transition: all 0.12s ease;
  }}
  div.stButton > button[kind="primary"]:hover {{
    background: {BRAND_RED};
    box-shadow: 6px 6px 0 {BRAND_NAVY};
    transform: translate(-2px, -2px);
  }}
  /* secondary 버튼 */
  div.stButton > button[kind="secondary"] {{
    background: white; color: {BRAND_NAVY};
    border: 3px solid {BRAND_NAVY};
    box-shadow: 3px 3px 0 {BRAND_NAVY};
    font-family: 'Archivo Black', 'Pretendard', sans-serif;
    font-weight: 700; letter-spacing: 0.02em;
    border-radius: 0;
  }}
  div.stButton > button[kind="secondary"]:hover {{
    background: {BRAND_YELLOW};
    box-shadow: 5px 5px 0 {BRAND_NAVY};
    transform: translate(-2px, -2px);
  }}
  /* 사이드바 강조 박스 */
  .of-scope-box {{
    background: white; border: 3px solid {BRAND_NAVY};
    box-shadow: 4px 4px 0 {BRAND_NAVY};
    padding: 14px; font-size: 13px; line-height: 1.55;
    color: {BRAND_NAVY}; margin-bottom: 14px;
  }}
  .of-scope-box .of-scope-title {{
    font-family: 'Archivo Black', sans-serif;
    font-size: 10px; letter-spacing: 0.08em;
    color: {BRAND_RED}; margin-bottom: 8px;
  }}
  .of-scope-box .of-scope-num {{
    font-family: 'Archivo Black', sans-serif;
    font-size: 18px; color: {BRAND_NAVY};
  }}
  /* 검색 입력 박스 — Brutalist */
  .stTextInput > div > div > input {{
    font-size: 15px; padding: 14px 18px;
    border-radius: 0; border: 3px solid {BRAND_NAVY};
    box-shadow: 4px 4px 0 {BRAND_NAVY};
    background: white; font-weight: 500;
    transition: all 0.12s ease;
  }}
  .stTextInput > div > div > input:focus {{
    border-color: {BRAND_RED};
    box-shadow: 6px 6px 0 {BRAND_RED};
    outline: none;
  }}
  /* 사이드바 BG 흰 */
  section[data-testid="stSidebar"] {{
    background: white !important;
    border-right: 3px solid {BRAND_NAVY};
  }}
  /* Expander — Brutalist */
  div[data-testid="stExpander"] {{
    border: 3px solid {BRAND_NAVY} !important;
    box-shadow: 4px 4px 0 {BRAND_NAVY};
    border-radius: 0 !important;
    background: white;
    margin-bottom: 16px;
  }}
  /* 컨테이너(border=True) */
  div[data-testid="stVerticalBlockBorderWrapper"] {{
    border: 3px solid {BRAND_NAVY} !important;
    box-shadow: 5px 5px 0 {BRAND_NAVY};
    border-radius: 0 !important;
    background: white;
  }}
  hr {{ border-top: 3px solid {BRAND_NAVY} !important; }}
</style>
""", unsafe_allow_html=True)

# 반응형 CSS — 단순 raw string, 큰 박스주석/한글 주석 안 씀 (streamlit 처리 깨짐 회피)
st.markdown("""
<style>
h1, h2, h3 {
  font-family: 'Archivo Black', 'Pretendard', sans-serif !important;
  color: #0a0a0a; letter-spacing: -0.02em;
}
div[data-testid="stHorizontalBlock"] div.stButton > button {
  height: 50px; min-width: 0; padding: 0 14px; font-size: 18px; white-space: nowrap;
}
.of-brand-slim {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 0 8px 0; border-bottom: 2px solid #0a0a0a; margin-bottom: 10px;
}
.of-brand-slim .of-logo-box { font-size: 16px !important; padding: 4px 8px !important; }
.of-brand-slim .of-logo { font-size: 20px !important; }
.of-brand-slim .of-badge { font-size: 9px !important; padding: 3px 7px !important; }
.of-version-inline {
  margin-left: auto; font-family: 'Archivo Black', monospace;
  font-size: 10px; color: #6a6a6a; letter-spacing: 0.08em;
}
/* 검색 form — 모던 glass 스타일 (라운드 + 반투명 + subtle shadow) */
.of-search-float {
  position: fixed !important;
  top: 22px !important;
  left: 50% !important;
  transform: translateX(-50%) !important;
  width: 420px !important;
  max-width: calc(100vw - 28px) !important;
  z-index: 150 !important;
  background: rgba(255, 255, 255, 0.85) !important;
  border: 1px solid rgba(15, 23, 42, 0.08) !important;
  border-radius: 28px !important;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.12),
              0 2px 6px rgba(15, 23, 42, 0.05) !important;
  padding: 5px 6px 5px 18px !important;
  height: 52px !important;
  overflow: visible !important;
  backdrop-filter: blur(14px) saturate(1.4);
  -webkit-backdrop-filter: blur(14px) saturate(1.4);
  transition: box-shadow 0.18s ease, border-color 0.18s ease;
}
.of-search-float:focus-within {
  box-shadow: 0 14px 38px rgba(15, 23, 42, 0.16),
              0 0 0 4px rgba(220, 38, 38, 0.10) !important;
  border-color: rgba(220, 38, 38, 0.35) !important;
}
.of-search-float [data-testid="stVerticalBlock"],
.of-search-float [data-testid="stVerticalBlockBorderWrapper"],
.of-search-float > div {
  position: relative !important;
  display: flex !important;
  flex-direction: row !important;
  align-items: center !important;
  gap: 0 !important;
  width: 100% !important;
  height: 100% !important;
  padding: 0 !important;
  margin: 0 !important;
}
.of-search-float [data-testid="element-container"],
.of-search-float .element-container {
  flex: 1 1 auto !important;
  margin: 0 !important;
  padding: 0 !important;
  width: auto !important;
}
/* input — border 없음, 투명 배경, 부드러운 글자 */
.of-search-float input {
  padding: 0 50px 0 0 !important;
  font-size: 14.5px !important;
  height: 42px !important;
  width: 100% !important;
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  outline: none !important;
  color: #0f172a !important;
  font-weight: 500;
  letter-spacing: -0.01em;
}
.of-search-float input::placeholder {
  color: #94a3b8 !important;
  font-weight: 400;
}
/* streamlit input wrapper 배경도 제거 */
.of-search-float div[data-baseweb="input"],
.of-search-float div[data-baseweb="base-input"] {
  background: transparent !important;
  border: none !important;
}
/* submit button — 우측에 작은 원형 빨강 */
.of-search-float [data-testid="stFormSubmitButton"] {
  position: absolute !important;
  right: 4px !important;
  top: 50% !important;
  transform: translateY(-50%) !important;
  z-index: 5;
  margin: 0 !important;
  flex: 0 0 auto !important;
  width: auto !important;
}
.of-search-float [data-testid="stFormSubmitButton"] button {
  height: 38px !important;
  width: 38px !important;
  min-width: 0 !important;
  padding: 0 !important;
  font-size: 16px !important;
  border-radius: 50% !important;
  background: #dc2626 !important;
  color: white !important;
  border: none !important;
  box-shadow: 0 2px 8px rgba(220, 38, 38, 0.35) !important;
  transition: background-color 0.12s ease, box-shadow 0.12s ease !important;
}
/* hover: 위치는 그대로, 색만 진하게 (사용자 요청: 위치 고정) */
.of-search-float [data-testid="stFormSubmitButton"] button:hover {
  background: #b91c1c !important;
  box-shadow: 0 4px 14px rgba(220, 38, 38, 0.55) !important;
}
.of-search-float [data-testid="stFormSubmitButton"] button:active {
  background: #991b1b !important;
}

/* 검색 로딩 오버레이 — 지루하지 않게 풀스크린 펄스 + 진행 텍스트 */
.of-loading-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.55);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  z-index: 9999;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 22px;
  animation: of-fade-in 0.25s ease;
}
@keyframes of-fade-in { from { opacity: 0; } to { opacity: 1; } }
.of-loading-rings {
  position: relative;
  width: 110px;
  height: 110px;
}
.of-loading-rings div {
  position: absolute;
  inset: 0;
  border: 5px solid transparent;
  border-top-color: #dc2626;
  border-radius: 50%;
  animation: of-spin 1.1s cubic-bezier(0.5, 0, 0.5, 1) infinite;
}
.of-loading-rings div:nth-child(2) {
  inset: 14px;
  border-top-color: #fbbf24;
  animation-duration: 0.8s;
  animation-direction: reverse;
}
.of-loading-rings div:nth-child(3) {
  inset: 28px;
  border-top-color: white;
  animation-duration: 1.4s;
}
@keyframes of-spin { to { transform: rotate(360deg); } }
.of-loading-text {
  color: white;
  font-family: 'Archivo Black', 'Pretendard', sans-serif;
  font-size: 18px;
  letter-spacing: 0.04em;
  text-align: center;
}
.of-loading-sub {
  color: rgba(255,255,255,0.7);
  font-size: 12.5px;
  letter-spacing: 0.04em;
  font-family: 'Pretendard', sans-serif;
  animation: of-pulse 1.6s ease-in-out infinite;
}
@keyframes of-pulse {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}
/* streamlit 기본 spinner는 숨김 (우리 커스텀 사용) */
div[data-testid="stSpinner"] { display: none !important; }
/* 검색 input 내부에 아이콘 — input wrapper에 relative position 부여 */
.of-search-icon-form > div { position: relative !important; }
.of-search-icon-form .stTextInput > div > div > input {
  padding-right: 48px !important;
}
.of-search-icon-form .stFormSubmitButton,
.of-search-icon-form div[data-testid="stFormSubmitButton"] {
  position: absolute !important;
  right: 4px !important;
  top: 50% !important;
  transform: translateY(-50%) !important;
  z-index: 5;
}
.of-search-icon-form .stFormSubmitButton button,
.of-search-icon-form div[data-testid="stFormSubmitButton"] button {
  height: 38px !important;
  min-width: 0 !important;
  padding: 0 12px !important;
  font-size: 16px !important;
  box-shadow: 2px 2px 0 #0a0a0a !important;
}

/* 사이드바 완전 제거 — 시안 4 */
section[data-testid="stSidebar"] { display: none !important; }
button[data-testid="stSidebarToggleButton"] { display: none !important; }
button[data-testid="stExpandSidebarButton"] { display: none !important; }
button[data-testid="stSidebarCollapseButton"] { display: none !important; }
button[data-testid="collapsedControl"] { display: none !important; }
button[kind="header"] { display: none !important; }
div[data-testid="stSidebarCollapsedControl"] { display: none !important; }
/* streamlit 자체 header/footer 제거 — 진짜 풀스크린 위해 */
header[data-testid="stHeader"] { display: none !important; }
div[data-testid="stHeader"] { display: none !important; }
footer { display: none !important; }
div[data-testid="stStatusWidget"] { display: none !important; }
.viewerBadge_link__1S137 { display: none !important; }
#MainMenu { display: none !important; }
/* 헤더(.of-brand-slim) 제거 — 시안 4 최소 UI */
.of-brand-slim { display: none !important; }
/* body/html 자체 viewport 고정 (스크롤 제거) */
body, html { overflow: hidden !important; height: 100vh !important; }

/* 결과 영역(GPT 카드 + 시세 요약 + 평단가 caption) — 지도 위 좌측 floating */
.of-summary-overlay {
  position: fixed;
  left: 14px;
  top: 220px;
  width: 380px;
  max-height: calc(100vh - 240px);
  overflow-y: auto;
  z-index: 100;
  background: rgba(254, 249, 195, 0.95);
  border: 3px solid #0a0a0a;
  box-shadow: 5px 5px 0 #dc2626;
  padding: 12px 14px;
  backdrop-filter: blur(3px);
}
.of-summary-overlay > .element-container { width: 100% !important; }
.of-summary-overlay div[data-testid="stMetric"] {
  padding: 6px 8px !important;
  border: 2px solid #0a0a0a !important;
  background: white !important;
  margin: 4px 0 !important;
}
.of-summary-overlay div[data-testid="stMetricValue"] { font-size: 16px !important; }
.of-summary-overlay div[data-testid="stMetricLabel"] { font-size: 10px !important; }
.of-summary-overlay .of-gpt-card {
  padding: 10px 12px !important; margin: 0 0 10px 0 !important;
  font-size: 12.5px !important; box-shadow: 4px 4px 0 #dc2626 !important;
}
.of-summary-overlay h2, .of-summary-overlay h3 {
  font-size: 14px !important; margin: 8px 0 4px 0 !important;
}

/* PC (>=769px) — 시안 4 풀스크린 컨셉 */
@media (min-width: 769px) {
  section.main > div.block-container {
    padding: 0 !important;
    max-width: 100vw !important;
  }
  /* main이 viewport 100% 차지 */
  section.main {
    margin-left: 0 !important;
    width: 100% !important;
  }
  /* 헤더(of-brand-slim)를 지도 위 fixed 띠로 */
  .of-brand-slim {
    position: fixed !important;
    top: 0 !important; left: 0 !important; right: 0 !important;
    z-index: 120 !important;
    background: rgba(254, 243, 199, 0.95) !important;
    backdrop-filter: blur(3px);
    padding: 6px 18px !important;
    margin: 0 !important;
    border-bottom: 2px solid #0a0a0a !important;
  }
  /* 지도 iframe — 진짜 풀스크린 100vh */
  iframe[height="540"] {
    height: 100vh !important; min-height: 100vh !important;
  }
  iframe[height="490"] {
    height: calc(100vh - 60px) !important; min-height: 490px;
  }
  /* 지도 element-container도 margin 0 (헤더 fixed 위에 깔리지 않게 top: 0) */
  iframe[height="540"] {
    margin-top: 0 !important;
  }
  .of-gpt-card {
    padding: 12px 16px !important; margin: 8px 0 12px 0 !important;
    font-size: 13px !important;
  }
  h2, h3 { margin-top: 0.4rem !important; margin-bottom: 0.4rem !important; }
  div[data-testid="stMetric"] { padding: 4px 8px !important; }
  /* 드로어 핸들 PC에서도 표시 */
  #of-drawer-handle { display: flex !important; }
  /* col_table = 우측 드로어 (PC에도 적용) */
  .of-drawer-container {
    position: fixed !important;
    top: 0 !important; right: 0 !important;
    width: 440px !important; height: 100vh !important;
    background: white !important;
    z-index: 9000 !important;
    border-left: 3px solid #0a0a0a !important;
    box-shadow: -8px 0 16px rgba(0,0,0,0.18) !important;
    transform: translateX(100%);
    transition: transform 0.28s ease;
    overflow-y: auto;
    padding: 14px 12px;
  }
  .of-drawer-container.open { transform: translateX(0); }
}

/* 모바일 (<=768px) */
@media (max-width: 768px) {
  .of-logo { font-size: 22px !important; }
  .of-logo-box { font-size: 18px !important; padding: 5px 9px !important; }
  .of-brand { flex-wrap: wrap; gap: 8px !important; }
  .of-badge { font-size: 10px !important; padding: 3px 7px !important; }
  .of-brand-sub { font-size: 12px !important; }
  .block-container { padding-top: 1rem !important; padding-left: 0.5rem !important;
                     padding-right: 0.5rem !important; }
  .stTextInput > div > div > input {
    font-size: 14px !important; padding: 11px 12px !important;
  }
  div.stButton > button {
    font-size: 14px !important; padding: 10px 12px !important; height: 46px !important;
  }
  div[data-testid="stHorizontalBlock"] div.stButton > button {
    height: 46px !important; padding: 0 10px !important; font-size: 16px !important;
  }
  div[data-testid="stMetricValue"] { font-size: 16px !important; }
  div[data-testid="stMetricLabel"] { font-size: 10.5px !important; }
  div[role="dialog"] {
    max-width: 92vw !important; width: 92vw !important;
    max-height: 60vh !important; margin-top: 30vh !important;
  }
  div[role="dialog"] > div { padding: 10px 12px !important; }
  iframe[height="540"] { height: 72vh !important; min-height: 380px !important; }
  iframe[height="490"] { height: 70vh !important; }
  .of-narrow-wrap { max-width: 100% !important; }
}

/* 우측 드로어 핸들 — PC + 모바일 모두 표시 */
#of-drawer-handle {
  display: flex;
  position: fixed;
  top: 50%; right: 0;
  transform: translateY(-50%);
  background: #0a0a0a;
  color: #fbbf24;
  z-index: 9100;
  padding: 14px 8px;
  font-family: 'Archivo Black', sans-serif;
  font-size: 12px;
  letter-spacing: 0.05em;
  cursor: pointer;
  border-top-left-radius: 8px;
  border-bottom-left-radius: 8px;
  box-shadow: -3px 3px 0 rgba(0,0,0,0.3);
  writing-mode: vertical-rl;
  text-orientation: mixed;
  user-select: none;
}
</style>

<script>
// (참고) 이 script는 streamlit이 sanitize해서 실행 안 됨 — components.html()로 별도 처리
(function() {
  function applyDrawer() {
    var t = document.getElementById('of-tbl-anchor');
    if (!t) return;
    var col = t.parentElement;
    while (col && !(col.getAttribute &&
      (col.getAttribute('data-testid') === 'column' ||
       col.getAttribute('data-testid') === 'stColumn'))) {
      col = col.parentElement;
      if (!col || col === document.body) return;
    }
    if (col && !col.classList.contains('of-drawer-container')) {
      col.classList.add('of-drawer-container');
      window._ofDrawerCol = col;
    }
  }

  function applySummaryOverlay() {
    var start = document.getElementById('of-summary-start');
    var end = document.getElementById('of-summary-end');
    if (!start || !end) return;
    // marker의 element-container 부모 찾기
    function ecParent(el) {
      var p = el.parentElement;
      while (p && !(p.classList && p.classList.contains('element-container'))) {
        p = p.parentElement;
        if (!p || p === document.body) return null;
      }
      return p;
    }
    var startEc = ecParent(start);
    var endEc = ecParent(end);
    if (!startEc || !endEc) return;
    if (startEc.previousElementSibling
        && startEc.previousElementSibling.classList.contains('of-summary-overlay')) {
      // 이미 wrap됨 — 마커 사이 element를 wrapper로 옮기기만
      var wrapper = startEc.previousElementSibling;
      var node = startEc.nextElementSibling;
      while (node && node !== endEc) {
        var next = node.nextElementSibling;
        wrapper.appendChild(node);
        node = next;
      }
      return;
    }
    var wrapper = document.createElement('div');
    wrapper.className = 'of-summary-overlay';
    var parent = startEc.parentNode;
    parent.insertBefore(wrapper, startEc);
    var node = startEc.nextElementSibling;
    while (node && node !== endEc) {
      var next = node.nextElementSibling;
      wrapper.appendChild(node);
      node = next;
    }
    // marker 자체는 숨김
    startEc.style.display = 'none';
    endEc.style.display = 'none';
  }

  function applyNarrowWidth() {
    // 검색 form의 element-container max-width 520px
    var form = document.querySelector('form[data-testid="stForm"]');
    if (form) {
      var ec = form.closest('.element-container, [data-testid="element-container"]');
      if (ec) ec.style.maxWidth = '520px';
      form.style.maxWidth = '520px';
    }
    // 매물 검증 expander
    document.querySelectorAll('div[data-testid="stExpander"]').forEach(function(ex) {
      var ec = ex.closest('.element-container, [data-testid="element-container"]');
      if (ec) ec.style.maxWidth = '520px';
      ex.style.maxWidth = '520px';
    });
  }

  function applyAll() {
    try { applyDrawer(); } catch(e) {}
    try { applySummaryOverlay(); } catch(e) {}
    try { applyNarrowWidth(); } catch(e) {}
  }
  applyAll();
  setInterval(applyAll, 500);
  // 페이지 로드 직후에도 강제 (streamlit 첫 렌더 후)
  if (document.readyState === 'complete') {
    setTimeout(applyAll, 100);
  } else {
    window.addEventListener('load', function() { setTimeout(applyAll, 100); });
  }
})();
</script>

<div id="of-drawer-handle" onclick="
  var col = window._ofDrawerCol;
  if (!col) {
    var t = document.getElementById('of-tbl-anchor');
    if (t) {
      col = t.parentElement;
      while (col && !(col.getAttribute &&
        (col.getAttribute('data-testid') === 'column' ||
         col.getAttribute('data-testid') === 'stColumn'))) {
        col = col.parentElement;
        if (!col || col === document.body) { col = null; break; }
      }
    }
  }
  if (!col) return;
  if (!col.classList.contains('of-drawer-container')) {
    col.classList.add('of-drawer-container');
  }
  if (col.classList.contains('open')) {
    col.classList.remove('open');
    this.innerText = '📋 거래목록';
  } else {
    col.classList.add('open');
    this.innerText = '✕ 닫기';
  }
">📋 거래목록</div>
""", unsafe_allow_html=True)

# JS는 streamlit이 sanitize하니까 components.html() iframe으로 우회 — parent.document 접근
components.html("""
<script>
(function() {
  var doc = null;
  try { doc = window.parent.document; }
  catch(e) { try { doc = window.top.document; } catch(e2) {} }

  // 디버그 패널 — ?debug=1 URL일 때만 표시
  var _ofDebugEnabled = false;
  try {
    _ofDebugEnabled = (window.parent.location.search || '').indexOf('debug=1') >= 0;
  } catch(e) {}
  function dbg(msg) {
    if (!doc || !_ofDebugEnabled) return;
    var el = doc.getElementById('of-js-debug');
    if (!el) {
      el = doc.createElement('div');
      el.id = 'of-js-debug';
      el.style.cssText = 'position:fixed;bottom:8px;left:8px;background:#dc2626;'
        + 'color:white;padding:6px 10px;z-index:99999;font-size:11px;'
        + 'font-family:monospace;max-width:340px;border:2px solid #0a0a0a;'
        + 'box-shadow:2px 2px 0 #0a0a0a;line-height:1.4;';
      doc.body.appendChild(el);
    }
    el.innerHTML = msg;
  }
  if (!doc) { return; }

  function applyDrawer() {
    var t = doc.getElementById('of-tbl-anchor');
    if (!t) return;
    var col = t.parentElement;
    while (col && !(col.getAttribute &&
      (col.getAttribute('data-testid') === 'column' ||
       col.getAttribute('data-testid') === 'stColumn'))) {
      col = col.parentElement;
      if (!col || col === doc.body) return;
    }
    if (col && !col.classList.contains('of-drawer-container')) {
      col.classList.add('of-drawer-container');
      window.parent._ofDrawerCol = col;
    }
  }

  function applySummaryOverlay() {
    var start = doc.getElementById('of-summary-start');
    var end = doc.getElementById('of-summary-end');
    if (!start || !end) return;
    function ecParent(el) {
      var p = el.parentElement;
      while (p && !(p.classList && p.classList.contains('element-container'))) {
        p = p.parentElement;
        if (!p || p === doc.body) return null;
      }
      return p;
    }
    var startEc = ecParent(start);
    var endEc = ecParent(end);
    if (!startEc || !endEc) return;
    if (startEc.previousElementSibling
        && startEc.previousElementSibling.classList.contains('of-summary-overlay')) {
      var wrapper = startEc.previousElementSibling;
      var node = startEc.nextElementSibling;
      while (node && node !== endEc) {
        var next = node.nextElementSibling;
        wrapper.appendChild(node);
        node = next;
      }
      return;
    }
    var wrapper = doc.createElement('div');
    wrapper.className = 'of-summary-overlay';
    var parent = startEc.parentNode;
    parent.insertBefore(wrapper, startEc);
    var node = startEc.nextElementSibling;
    while (node && node !== endEc) {
      var next = node.nextElementSibling;
      wrapper.appendChild(node);
      node = next;
    }
    startEc.style.display = 'none';
    endEc.style.display = 'none';
  }

  function applyNarrowWidth() {
    // 매물 교차 검증 expander hide
    doc.querySelectorAll('div[data-testid="stExpander"]').forEach(function(ex) {
      var summary = ex.querySelector('summary');
      if (summary && summary.textContent.indexOf('매물 교차 검증') >= 0) {
        var ec = ex.closest('.element-container');
        if (ec) ec.style.display = 'none';
        else ex.style.display = 'none';
      }
    });
  }

  function applyFullscreenMap() {
    // 지도 iframe 자체를 position:fixed로 viewport 전체 차지 (z-index 1)
    // body overflow도 강제
    if (doc.body) {
      doc.body.style.overflow = 'hidden';
      doc.body.style.height = '100vh';
      doc.body.style.margin = '0';
    }
    if (doc.documentElement) {
      doc.documentElement.style.overflow = 'hidden';
      doc.documentElement.style.height = '100vh';
    }
    var nMaps = 0;
    doc.querySelectorAll('iframe').forEach(function(f) {
      var h = parseInt(f.getAttribute('height') || '0');
      var src = (f.src || '') + (f.title || '');
      var isMap = (h === 540) || (src.indexOf('naver') >= 0);
      if (isMap) {
        f.style.position = 'fixed';
        f.style.top = '0';
        f.style.left = '0';
        f.style.right = '0';
        f.style.bottom = '0';
        f.style.width = '100vw';
        f.style.height = '100vh';
        f.style.display = 'block';
        f.style.border = 'none';
        f.style.zIndex = '1';
        f.style.margin = '0';
        nMaps++;
      }
    });
    window._ofDbgMap = nMaps;
  }

  function applySearchFloat() {
    var form = doc.querySelector('form[data-testid="stForm"]')
      || doc.querySelector('div[data-testid="stForm"]')
      || doc.querySelector('section[data-testid="stForm"]');
    if (!form) { window._ofDbgForm = 'NO'; return; }
    window._ofDbgForm = 'YES (' + form.tagName + ')';
    // form 자체에 class 부여 (element-container 우회)
    if (!form.classList.contains('of-search-float')) {
      form.classList.add('of-search-float');
    }
    // 부모 element-container들의 min-height/margin 해제 (자리 안 차지)
    var p = form.parentElement;
    var depth = 0;
    while (p && p !== doc.body && depth < 5) {
      p.style.minHeight = '0';
      p.style.margin = '0';
      p.style.padding = '0';
      p = p.parentElement;
      depth++;
    }
  }

  function applyAll() {
    try { applyDrawer(); } catch(e) {}
    try { applySummaryOverlay(); } catch(e) {}
    try { applyNarrowWidth(); } catch(e) {}
    try { applySearchFloat(); } catch(e) {}
    try { applyFullscreenMap(); } catch(e) {}
    // 디버그 표시
    var nForms = doc.querySelectorAll('form').length;
    var nExp = doc.querySelectorAll('[data-testid="stExpander"]').length;
    var nIframe = doc.querySelectorAll('iframe').length;
    var float = doc.querySelector('.of-search-float') ? '✓' : '✗';
    var overlay = doc.querySelector('.of-summary-overlay') ? '✓' : '✗';
    var drawer = doc.querySelector('.of-drawer-container') ? '✓' : '✗';
    dbg('JS OK · form ' + nForms + ' · expander ' + nExp
      + ' · iframe ' + nIframe + '<br>float ' + float
      + ' · overlay ' + overlay + ' · drawer ' + drawer
      + '<br>map iframe 100vh: ' + (window._ofDbgMap || 0)
      + '<br>form찾기: ' + (window._ofDbgForm || '?'));
  }
  applyAll();
  setInterval(applyAll, 500);
})();
</script>
""", height=0)

# 사이드바 제거됨 (시안 4: 지도 풀스크린 컨셉)
# include_road 옵션은 hardcode (필요시 검색 form 안 옵션으로 추가 가능)
include_road = False

# 시안 4: PC에서는 슬림한 상단 띠 (로고+배지+버전만), 모바일에서는 크게
st.markdown(
    f"""
    <div class="of-brand of-brand-slim">
      <div class="of-logo-box">OF</div>
      <div>
        <div class="of-logo">One<span class="of-logo-accent">Family</span> 실거래가</div>
      </div>
<!-- 용인시 배지 제거 (사용자 요청) -->
      <span class="of-version-inline">{APP_VERSION} · {APP_RELEASE_DATE}</span>
    </div>
    """,
    unsafe_allow_html=True,
)

# 검색창 — 입력창 안에 작은 🔍 아이콘 (form submit 버튼을 absolute로 input 위에 오버레이)
# 폭은 좁게 (max-width: 520px)
st.markdown('<div class="of-narrow-wrap of-search-icon-form">',
            unsafe_allow_html=True)
with st.form(key="of_search_form", clear_on_submit=False, border=False):
    query = st.text_input(
        "질의", placeholder="자연어 한 줄 입력 (예: 두창리 957-5와 비슷한 조건)",
        label_visibility="collapsed",
        key="of_query_input",
    )
    go = st.form_submit_button("🔍", type="primary", help="검색")
st.markdown('</div>', unsafe_allow_html=True)

# 매물 교차 검증 — 사용자 요청으로 UI 완전 숨김 (디버깅용으로만 사용했음)
# 코드는 보존하되 hidden div로 감쌈
st.markdown('<div style="display:none;" class="of-narrow-wrap">', unsafe_allow_html=True)
with st.expander("📋 매물 교차 검증", expanded=False):
    st.markdown(
        f"<div style='font-size:13px;color:#475569;margin-bottom:8px;line-height:1.6;'>"
        f"네이버 부동산·디스코·밸류맵 등에서 본 매물 정보를 한 줄로 입력하면 "
        f"우리 DB의 후보 필지를 <b style='color:{BRAND_NAVY};'>일치도 점수</b>와 함께 보여줍니다. "
        f"PNU가 확정되면 그 필지의 모든 실거래·시세를 확인할 수 있어요."
        f"</div>",
        unsafe_allow_html=True,
    )
    listing_text = st.text_input(
        "매물 정보",
        placeholder=(
            "예: 백암면 백봉리 산98 임야 11,287㎡ 3,414평 "
            "공시지가 1㎡ 36만원 평당 121만원"
        ),
        label_visibility="collapsed",
        key="of_listing_input",
    )
    listing_go = st.button(
        "🔎 우리 DB에서 후보 찾기", type="secondary",
        use_container_width=True, key="of_listing_btn",
    )
    if listing_go and listing_text:
        with st.spinner("매물 정보 파싱 + DB 후보 lookup..."):
            try:
                client = get_client()
                parsed = parse_listing(client, listing_text)
            except Exception as e:
                st.error(f"파싱 오류: {type(e).__name__}: {e}")
                parsed = None
            if parsed:
                # 파싱 결과 미리 보기
                pretty = []
                if parsed.get("emd"): pretty.append(f"📍 {parsed['emd']}")
                if parsed.get("ri"): pretty.append(parsed["ri"])
                if parsed.get("jibun"):
                    pretty.append(f"지번 **{parsed['jibun']}**")
                if parsed.get("jimok"):
                    pretty.append(f"지목 **{parsed['jimok']}**")
                if parsed.get("area_m2"):
                    pretty.append(f"{parsed['area_m2']:,.0f}㎡")
                if parsed.get("area_pyeong"):
                    pretty.append(f"{parsed['area_pyeong']:,.0f}평")
                if parsed.get("jiga_per_m2"):
                    pretty.append(f"공시 {parsed['jiga_per_m2']:,.0f}원/㎡")
                st.caption(" · ".join(pretty) if pretty else "(인식된 필드 없음)")

                # DB 후보 lookup
                conn_listing = get_conn()
                candidates = find_parcel_candidates(conn_listing, parsed)
                st.session_state._of_listing_candidates = candidates
                st.session_state._of_listing_parsed = parsed
            else:
                st.session_state._of_listing_candidates = None

    # 결과 카드 (rerun에도 보존)
    candidates = st.session_state.get("_of_listing_candidates")
    if candidates is not None:
        if not candidates:
            st.warning(
                "조건에 맞는 후보가 없어요. 더 자세한 정보를 입력하거나 "
                "지번·면적·공시지가 중 하나만 입력해보세요."
            )
        else:
            st.markdown(
                f"<div style='margin-top:8px;font-weight:600;font-size:13px;"
                f"color:{BRAND_NAVY_DEEP};'>📊 일치도 상위 후보 "
                f"{len(candidates)}개</div>",
                unsafe_allow_html=True,
            )
            for i, (score, breakdown, parc) in enumerate(candidates):
                py = parc["area_m2"] / PYEONG_PER_M2 if parc["area_m2"] else 0
                pct = min(100, int(score))  # 최대 100점
                card_color = (BRAND_NAVY if pct >= 80
                              else "#f59e0b" if pct >= 50
                              else "#94a3b8")
                with st.container(border=True):
                    head_cols = st.columns([3, 1, 1])
                    with head_cols[0]:
                        st.markdown(
                            f"**{parc['jibun']}** "
                            f"<span style='color:#64748b;'>({parc['jimok']}, "
                            f"{int(parc['area_m2']):,}㎡ · {py:,.0f}평)</span>",
                            unsafe_allow_html=True,
                        )
                        sub_bits = [f"PNU `{parc['pnu']}`"]
                        if parc.get("jiga"):
                            sub_bits.append(
                                f"공시 {int(parc['jiga']):,}원/㎡")
                        if parc.get("shape_type"):
                            sub_bits.append(f"형상 {parc['shape_type']}")
                        if parc.get("has_road_access") == 1:
                            sub_bits.append("도로 접면")
                        elif parc.get("has_road_access") == 0:
                            sub_bits.append("맹지")
                        st.caption(" · ".join(sub_bits))
                        st.caption("점수: " + " · ".join(breakdown))
                    with head_cols[1]:
                        st.markdown(
                            f"<div style='text-align:center;padding:8px 0;'>"
                            f"<div style='font-size:11px;color:#64748b;"
                            f"text-transform:uppercase;letter-spacing:0.04em;'>"
                            f"일치도</div>"
                            f"<div style='font-size:24px;font-weight:700;"
                            f"color:{card_color};'>{pct}%</div></div>",
                            unsafe_allow_html=True,
                        )
                    with head_cols[2]:
                        if st.button(
                            "📄 거래 보기",
                            key=f"of_listing_pick_{parc['pnu']}",
                            use_container_width=True,
                        ):
                            # 그 PNU 선택 → 메인 dialog 자동 open
                            st.session_state.selected_pnu = parc["pnu"]
                            st.session_state._dialog_shown_for = None
                            # 검색 결과 없어도 PNU 정보·거래는 sidebar에서 안 보이므로
                            # result도 비워두고 dialog만 띄움
                            # 단 dialog는 결과 영역 안에서만 호출되므로
                            # 사용자에게 안내
                            st.toast(
                                f"선택됨: {parc['jibun']}  "
                                f"아래 결과 영역의 dialog 또는 새 검색 시 표시됩니다."
                            )
                            st.rerun()
st.markdown('</div>', unsafe_allow_html=True)  # /of-narrow-wrap (매물검증)

if go and query:
    # 풀스크린 로딩 오버레이 (3겹 회전 링 + 텍스트)
    loading_ph = st.empty()
    loading_ph.markdown("""
    <div class="of-loading-overlay">
      <div class="of-loading-rings"><div></div><div></div><div></div></div>
      <div class="of-loading-text">🔍 분석 중</div>
      <div class="of-loading-sub">자연어를 조건으로 변환하고 DB에서 필지·거래를 매칭합니다...</div>
    </div>
    """, unsafe_allow_html=True)
    try:
        result = search_pipeline(query, include_road_jimok=include_road)
    except Exception as e:
        loading_ph.empty()
        st.error(f"검색 오류: {type(e).__name__}: {e}")
        st.stop()
    loading_ph.empty()
    st.session_state.result = result
    st.session_state.selected_pnu = None
    st.session_state.last_map_click_sig = None
    st.session_state.last_table_rows = []
    # 검색이 새로 일어났을 때만 지도 중심 재설정 (지도 컴포넌트가 recenter=True 받음)
    st.session_state._of_recenter_pending = True
    st.session_state._of_last_map_click_ts = None
    st.session_state.map_key = f"map_{datetime.now().timestamp()}"

# 결과 표시
if "result" not in st.session_state:
    # 검색 전: 빈 지도만 풀스크린으로 미리 표시 (용인시 가운데)
    of_naver_map(
        client_id=NAVER_MAP_CLIENT_ID,
        center=[37.21, 127.20],   # 용인시 처인구·기흥구·수지구 가운데
        zoom=11,
        markers=[], polygons=[],
        road_lines=None,
        sel_color=SELECTED_COLOR,
        zoom_label_threshold=15,
        recenter=True,
        key="of_naver_map_empty",
    )

if "result" in st.session_state:
    result = st.session_state.result
    cond = result["cond"]
    results = result["results"]

    if result["out_of_range"]:
        st.warning(
            "⚠️ 검색 가능 지역은 **용인시 3개구**(처인·기흥·수지, 42개 읍·면·동)입니다. "
            "그 외 지역은 무시되었습니다."
        )

    # 결과 영역 시작 marker (JS로 wrapping하여 좌측 floating overlay로)
    st.markdown('<div id="of-summary-start"></div>', unsafe_allow_html=True)

    # GPT 스타일 응답 카드 — 검색 의도 자연어 풀이 + 항목 칩
    sentence, chips = format_cond_as_sentence(cond, result)
    chip_html = "".join(
        f'<span style="display:inline-block;background:{BRAND_NAVY_LIGHT};'
        f'color:{BRAND_NAVY_DEEP};padding:4px 10px;border-radius:999px;'
        f'font-size:12px;font-weight:500;margin:3px 4px 3px 0;'
        f'border:1px solid #d4dcef;">'
        f'<span style="opacity:0.7;margin-right:4px;">{label}</span>'
        f'<b>{value}</b></span>'
        for label, value in chips
    )
    st.markdown(
        f"""
        <div class="of-gpt-card">
          <div class="of-gpt-title">
            <span class="of-gpt-icon">OF</span>
            OneFamily가 정리한 내용
          </div>
          <div style="margin-bottom:10px;">{sentence}</div>
          <div>{chip_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 자연어 파싱 결과 expander 제거 — matched_road/period 정보만 짧게
    if result["matched_road"]:
        ri = result["road_info"] or {}
        st.caption(
            f"🛣️ 도로 매핑: `{cond.get('road_query')}` → "
            f"**{result['matched_road']}** ({ri.get('confidence')})"
            + (f"  ·  {ri.get('reason')}" if ri.get("reason") else "")
        )
    if result.get("start_ymd"):
        st.caption(f"📅 기간: {result['start_ymd']} ~ {result['end_ymd']}")

    # 참조 필지 정보 (reference_jibun)
    if result.get("reference_info"):
        ri = result["reference_info"]
        ref = ri["ref"]
        py = ref["area_m2"] / PYEONG_PER_M2 if ref.get("area_m2") else 0
        with st.expander(f"📍 참조 필지: {ref['jibun']} ({ref['jimok']}, {py:,.0f}평)",
                          expanded=True):
            cols = st.columns(4)
            cols[0].metric("면적", f"{int(ref['area_m2']):,}㎡",
                           help=f"{py:,.0f}평")
            cols[1].metric("공시지가",
                           f"{int(ref['jiga']):,}원/㎡" if ref.get('jiga') else "—")
            cols[2].metric("해발",
                           f"{ref['elevation_m']:.0f}m" if ref.get('elevation_m') is not None else "—",
                           help=f"경사 {ref['slope_deg']:.1f}°" if ref.get('slope_deg') is not None else "")
            cols[3].metric("도로 접면",
                           "접면" if ref.get('has_road_access') == 1 else
                           ("맹지" if ref.get('has_road_access') == 0 else "—"))
            if ri.get("notes"):
                st.caption("**자동 채워진 조건**: " + " · ".join(ri["notes"]))

    if not results:
        st.warning("조건에 맞는 거래가 없어요. 다른 표현으로 시도해보세요.")
        st.stop()

    # 시세 요약 (평단가 평균만)
    st.subheader("💰 시세 요약")
    solo = [(d, r) for d, r in results
            if r["match_confidence"] == "high" and r.get("share_group") == "단독"]
    units = [r["unit_per_pyeong"] for _, r in solo if r["unit_per_pyeong"]]
    cols = st.columns(3)
    cols[0].metric("전체 거래", f"{len(results):,}건")
    cols[1].metric("정상 시세 표본", f"{len(solo)}건",
                    help="확정 매칭 + 단독매매 (공유지분 거래 제외)")
    if units:
        avg = sum(units) / len(units)
        cols[2].metric("평단가 평균", f"{avg:,.0f} 만원/평")
        prices = [r["deal_amount"] for _, r in solo]
        st.caption(
            f"평단가 범위 {min(units):,.0f} ~ {max(units):,.0f} 만원/평  ·  "
            f"거래금액 중앙값 **{statistics.median(prices):,.0f}만원**  ·  "
            f"정렬: `{result['sort_by']} {result['sort_order']}`"
        )
    else:
        cols[2].metric("평단가 평균", "—")

    # 결과 영역 끝 marker
    st.markdown('<div id="of-summary-end"></div>', unsafe_allow_html=True)
    st.divider()

    # 이전 rerun에서 결정된 selected_pnu (지도·표 그리기에 사용)
    prev_selected_pnu = st.session_state.get("selected_pnu")

    # 선택된 PNU의 거래 상세를 dialog(팝업)로 표시
    @st.dialog("📄 선택 필지 · 실거래 상세", width="large")
    def show_parcel_dialog(pnu):
        sel_conn2 = get_conn()
        sel_parcel2 = sel_conn2.execute(
            """
            SELECT pnu, jibun, jimok, area_m2, jiga, addr,
                   elevation_m, slope_deg, has_road_access,
                   shape_type, shape_aspect,
                   zone_type, zone_detail,
                   road_frontage_m, road_frontage_ratio, road_n_sides,
                   is_corner_lot, road_frontage_angle_deg,
                   prefix8
            FROM parcels WHERE pnu = ?
            """,
            (pnu,),
        ).fetchone()
        sel_trades2 = list(sel_conn2.execute(
            """
            SELECT id, sigg_cd, umd_name, jimok, area_m2, jibun_masked, is_san,
                   deal_amount, deal_year, deal_month, deal_day, deal_ymd,
                   land_use, dealing_gbn,
                   match_confidence, resolved_pnu, resolved_jibun,
                   resolved_area_m2, resolved_lon, resolved_lat, resolved_jiga,
                   unit_per_pyeong, candidates_count, share_label,
                   price_anomaly
            FROM trades WHERE resolved_pnu = ?
            ORDER BY deal_ymd DESC
            """,
            (pnu,),
        ))
        if sel_parcel2:
            py = (sel_parcel2["area_m2"] / PYEONG_PER_M2
                  if sel_parcel2["area_m2"] else 0)
            st.markdown(
                f"### **{sel_parcel2['jibun']}** "
                f"({sel_parcel2['jimok']}, "
                f"{int(sel_parcel2['area_m2']):,}㎡ · {py:,.0f}평)"
            )
            addr = sel_parcel2["addr"] or ""
            sub_bits = []
            if addr: sub_bits.append(addr)
            sub_bits.append(f"PNU `{pnu}`")
            sub_bits.append(f"거래 **{len(sel_trades2)}건** 매칭")
            st.caption(" · ".join(sub_bits))

            # ━━━ 실거래 핵심 정보 (메인) ━━━
            unit_prices = [t["unit_per_pyeong"] for t in sel_trades2
                          if t["unit_per_pyeong"]]
            deal_amounts = [t["deal_amount"] for t in sel_trades2
                           if t["deal_amount"]]
            latest = sel_trades2[0] if sel_trades2 else None  # 최신순 정렬됨

            st.markdown(
                f"<div style='margin-top:14px;font-family:\"Archivo Black\",sans-serif;"
                f"font-size:11px;color:{BRAND_RED};letter-spacing:0.08em;"
                f"margin-bottom:6px;'>실거래 정보 · DEAL HISTORY</div>",
                unsafe_allow_html=True,
            )
            deal_cols = st.columns(3)
            # 1) 최근 거래가 — 억 단위
            if latest:
                amt_man = latest["deal_amount"] or 0
                if amt_man >= 10000:
                    eok = amt_man / 10000
                    amt_display = f"{eok:,.2f} 억"
                else:
                    amt_display = f"{amt_man:,} 만원"
                deal_cols[0].metric(
                    "최근 거래가", amt_display,
                    help=(f"{latest['deal_ymd'][:10]} 거래 · "
                          f"전체 {len(sel_trades2)}건"),
                )
            else:
                deal_cols[0].metric("최근 거래가", "—")
            # 2) 평수 (면적)
            py = (sel_parcel2["area_m2"] / PYEONG_PER_M2
                  if sel_parcel2["area_m2"] else 0)
            deal_cols[1].metric(
                "평수", f"{py:,.0f} 평",
                help=f"{int(sel_parcel2['area_m2']):,}㎡" if sel_parcel2["area_m2"] else None,
            )
            # 3) 평단가 (만원/평) — 최근 거래 기준, 없으면 평균
            if latest and latest["unit_per_pyeong"]:
                unit_disp = f"{int(latest['unit_per_pyeong']):,} 만원/평"
                unit_help = f"최근 거래 기준 ({latest['deal_ymd'][:10]})"
            elif unit_prices:
                avg_u = sum(unit_prices) / len(unit_prices)
                unit_disp = f"{avg_u:,.0f} 만원/평"
                unit_help = f"평균 ({len(unit_prices)}건)"
            else:
                unit_disp = "—"
                unit_help = None
            deal_cols[2].metric("평단가", unit_disp, help=unit_help)

            # ━━━ 입지 (보조) ━━━
            st.markdown(
                f"<div style='margin-top:18px;font-family:\"Archivo Black\",sans-serif;"
                f"font-size:11px;color:{BRAND_RED};letter-spacing:0.08em;"
                f"margin-bottom:6px;'>입지 · LOCATION</div>",
                unsafe_allow_html=True,
            )
            loc_cols = st.columns(4)
            loc_cols[0].metric(
                "해발",
                f"{sel_parcel2['elevation_m']:.0f}m"
                if sel_parcel2["elevation_m"] is not None else "—",
            )
            loc_cols[1].metric(
                "경사",
                f"{sel_parcel2['slope_deg']:.1f}°"
                if sel_parcel2["slope_deg"] is not None else "—",
            )
            # 도로 접면: 접도 길이까지 표시
            front_m = sel_parcel2["road_frontage_m"]
            n_sides = sel_parcel2["road_n_sides"]
            is_corner = sel_parcel2["is_corner_lot"]
            if front_m is not None and front_m > 0:
                access_label = "접면"
                if is_corner:
                    access_label = f"코너({n_sides}면)"
                elif n_sides == 1:
                    access_label = "1면 접도"
                access_help = f"접도 길이 {front_m:.1f}m · {n_sides}면"
            elif sel_parcel2["has_road_access"] == 1:
                access_label = "접면"
                access_help = None
            elif sel_parcel2["has_road_access"] == 0 or front_m == 0:
                access_label = "맹지"
                access_help = None
            else:
                access_label = "—"
                access_help = None
            loc_cols[2].metric(
                "도로 접면", access_label,
                help=access_help,
            )
            # 접도 길이 (있으면 표시)
            if front_m is not None and front_m > 0:
                loc_cols[3].metric(
                    "접도 길이", f"{front_m:.0f}m",
                    help="필지 외곽환 변 중 도로 ≤10m, 도로방향 평행 변의 길이 합",
                )
            else:
                loc_cols[3].metric("접도 길이", "—")

            # ━━━ 필지 속성 (작게, 보조) ━━━
            jiga_text = (f"{int(sel_parcel2['jiga']):,}원/㎡"
                         if sel_parcel2['jiga'] else "—")
            # 용도지역: zone_detail이 있으면 detail, 없으면 zone_type
            zone_text = (sel_parcel2["zone_detail"] or
                         sel_parcel2["zone_type"] or "—")
            attr_bits = [
                f"공시지가 <b>{jiga_text}</b>",
                f"형상 <b>{sel_parcel2['shape_type'] or '—'}</b>",
                f"용도지역 <b>{zone_text}</b>",
            ]
            st.markdown(
                f"<div style='margin-top:14px;padding:10px 14px;"
                f"background:#fef3c7;border:2px solid {BRAND_NAVY};"
                f"font-size:12.5px;color:{BRAND_NAVY};'>"
                f"📐 필지 속성 · "
                + " &nbsp;·&nbsp; ".join(attr_bits)
                + "</div>",
                unsafe_allow_html=True,
            )

            # 형상 의심 경고 — 매우 길쭉한 작은 임야는 V-World가 임야로 분류해도
            # 실제 도로/구거일 가능성이 있음 (J 같은 권리분석자에게 중요한 신호)
            asp = sel_parcel2["shape_aspect"]
            if (asp is not None and asp > 0
                    and asp < 0.2
                    and sel_parcel2["area_m2"]
                    and sel_parcel2["area_m2"] < 100
                    and sel_parcel2["jimok"] == "임야"):
                ratio = 1.0 / asp
                st.warning(
                    f"⚠️ **형상 의심** — 폴리곤 길이/너비 비율 약 1:{ratio:.1f}로 "
                    f"매우 길쭉하고 면적이 작아요({int(sel_parcel2['area_m2'])}㎡). "
                    f"V-World 분류는 **'{sel_parcel2['jimok']}'**이지만 "
                    f"실제는 **도로·구거·하천 가능성**도 있어요. "
                    f"현장 확인·등기부 확인을 권장합니다."
                )
        else:
            st.markdown(f"### PNU `{pnu}`")

        st.divider()
        st.markdown(
            f"<div style='font-weight:600;color:{BRAND_NAVY_DEEP};"
            f"font-size:13.5px;margin-bottom:6px;'>"
            f"📋 국토부 토지매매 실거래 원본 데이터</div>",
            unsafe_allow_html=True,
        )
        def _explain_match(t):
            """매칭 신뢰도 산출 근거를 사람이 읽을 수 있는 문장 리스트로."""
            bits = []
            masked = t["jibun_masked"] or ""
            resolved = t["resolved_jibun"] or ""
            if "*" in masked and resolved:
                bits.append(f"지번 별표 패턴 `{masked}` 복원 → **{resolved}**")
            elif masked and masked == resolved:
                bits.append(f"지번 **{resolved}** 정확 일치")
            elif resolved:
                bits.append(f"지번 복원: **{resolved}**")
            if t["is_san"]:
                bits.append("산(山) 구분 일치")
            if t["area_m2"] and t["resolved_area_m2"]:
                ref_a = float(t["resolved_area_m2"])
                deal_a = float(t["area_m2"])
                if ref_a > 0:
                    diff = abs(deal_a - ref_a) / ref_a * 100
                    if diff < 1:
                        bits.append(f"면적 거의 동일 (±{diff:.1f}%)")
                    elif diff < 5:
                        bits.append(f"면적 ±{diff:.1f}% (정상 범위)")
                    else:
                        bits.append(f"면적 ±{diff:.1f}% (약간 차이)")
            bits.append(f"지목 **{t['jimok']}** 일치")
            cands = t["candidates_count"] or 0
            if cands == 1:
                bits.append("후보 **1개 (유일 매칭)**")
            elif cands <= 3:
                bits.append(f"후보 {cands}개 중 1위 (점수 큰 차이)")
            elif cands > 0:
                bits.append(f"후보 {cands}개 중 1위 (지번·면적·공시지가 종합)")
            return bits

        CONF_COLOR = {"high": "#16a34a", "mid": "#ca8a04", "low": "#dc2626"}
        CONF_LABEL = {"high": "확정 매칭", "mid": "중간 신뢰", "low": "낮은 신뢰"}

        for i, t in enumerate(sel_trades2):
            anomaly_tag = ""
            if t["price_anomaly"] == "high_outlier":
                anomaly_tag = "  🔥고평가"
            elif t["price_anomaly"] == "low_outlier":
                anomaly_tag = "  ❄️저평가"
            with st.expander(
                f"#{i+1}  {t['deal_ymd'][:10]}  "
                f"{t['deal_amount']:,}만원  "
                f"{t['area_m2']:,.0f}㎡  "
                f"mask=`{t['jibun_masked']}`  "
                f"[{t['match_confidence']}]{anomaly_tag}",
                expanded=(i == 0),
            ):
                # 매칭 신뢰도 근거 박스 (왜 이 거래가 이 필지로 매칭됐는지)
                conf = t["match_confidence"] or "—"
                color = CONF_COLOR.get(conf, "#64748b")
                conf_label = CONF_LABEL.get(conf, conf)
                why_bits = _explain_match(t)
                why_html = "".join(
                    f"<div style='font-size:13px;line-height:1.65;'>"
                    f"<span style='color:{color};font-weight:700;'>✓</span> {b}</div>"
                    for b in why_bits
                )
                st.markdown(
                    f"<div style='margin-bottom:14px;padding:12px 14px;"
                    f"background:#fef9c3;border-left:5px solid {color};"
                    f"border:2px solid {BRAND_NAVY};border-left-width:5px;'>"
                    f"<div style='font-family:\"Archivo Black\",sans-serif;"
                    f"font-size:11.5px;color:{color};letter-spacing:0.06em;"
                    f"margin-bottom:6px;'>매칭 신뢰도 · {conf_label.upper()} "
                    f"<span style='color:{BRAND_NAVY};'>(왜 이 필지인가?)</span></div>"
                    f"{why_html}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                raw_cols = st.columns(2)
                with raw_cols[0]:
                    st.markdown(
                        "<b style='font-size:12px;color:#475569;'>"
                        "▌ API 원본 (국토부)</b>",
                        unsafe_allow_html=True,
                    )
                    api_raw = {
                        "sggCd (시군구코드)": t["sigg_cd"],
                        "umdNm (법정동)": t["umd_name"],
                        "jimok (지목)": t["jimok"],
                        "dealArea (거래면적㎡)": t["area_m2"],
                        "jibun (지번-원본 별표)":
                            ("산 " if t["is_san"] else "") + (t["jibun_masked"] or ""),
                        "dealAmount (거래금액 만원)": t["deal_amount"],
                        "dealYear/Month/Day":
                            f"{t['deal_year']} / {t['deal_month']} / {t['deal_day']}",
                        "landUse (용도지역)": t["land_use"] or "—",
                        "dealingGbn (거래유형)": t["dealing_gbn"] or "—",
                    }
                    for k, v in api_raw.items():
                        st.markdown(
                            f"<div style='font-size:13px;line-height:1.7;'>"
                            f"<span style='color:#64748b;'>{k}</span>: "
                            f"<b>{v}</b></div>",
                            unsafe_allow_html=True,
                        )
                with raw_cols[1]:
                    st.markdown(
                        "<b style='font-size:12px;color:#475569;'>"
                        "▌ OneFamily 복원·매칭 결과</b>",
                        unsafe_allow_html=True,
                    )
                    of_data = {
                        "복원 지번": t["resolved_jibun"] or "—",
                        "PNU (19자리)": t["resolved_pnu"] or "—",
                        "매칭 신뢰도": t["match_confidence"],
                        "후보 수": t["candidates_count"],
                        "평단가": (f"{t['unit_per_pyeong']:,.0f} 만원/평"
                                  if t["unit_per_pyeong"] else "—"),
                        "필지 공시지가": (f"{t['resolved_jiga']:,.0f} 원/㎡"
                                        if t["resolved_jiga"] else "—"),
                        "공유지분 라벨": t["share_label"] or "정상매칭",
                        "시세 이상치": (
                            "🔥 고평가 의심" if t["price_anomaly"] == "high_outlier"
                            else "❄️ 저평가 의심" if t["price_anomaly"] == "low_outlier"
                            else "정상 범위"
                        ),
                    }
                    for k, v in of_data.items():
                        st.markdown(
                            f"<div style='font-size:13px;line-height:1.7;'>"
                            f"<span style='color:#64748b;'>{k}</span>: "
                            f"<b>{v}</b></div>",
                            unsafe_allow_html=True,
                        )

    # PNU 변경된 직후에만 dialog open (한 번만 트리거 → 사용자가 ✕로 닫을 수 있음)
    if prev_selected_pnu and \
            st.session_state.get("_dialog_shown_for") != prev_selected_pnu:
        st.session_state._dialog_shown_for = prev_selected_pnu
        show_parcel_dialog(prev_selected_pnu)

    # 시안 4: 지도가 main 전체. 표는 우측 드로어(of-drawer-container)로 빠짐.
    # 비율은 [1, 0.01] 정도로 col_table 거의 없앰 — JS가 fixed로 빼냄.
    col_map, col_table = st.columns([100, 1])

    # ===== 지도 (네이버) =====
    with col_map:
        st.subheader("📍 거래 위치 (네이버 지도)")

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

        # 네이버 지도용 데이터 직렬화 (라벨 포함)
        markers_data = []
        polygons_data = []
        for pnu, group in pnu_groups.items():
            d, r = group[0]
            is_sel = (pnu == prev_selected_pnu)
            jc = jimok_color(r["jimok"])
            html = build_html(group)

            # 라벨: "1,234평·50만/평·2025-12"
            pyeong = int(r["area_m2"] * PYEONG_PER_M2) if r["area_m2"] else 0
            unit = int(r["unit_per_pyeong"]) if r["unit_per_pyeong"] else None
            latest_ymd = max(
                (rg["deal_ymd"] for _, rg in group if rg.get("deal_ymd")),
                default=""
            )
            ym_short = latest_ymd[:7] if latest_ymd else ""
            parts = [f"{pyeong:,}평"]
            if unit:
                parts.append(f"{unit:,}만/평")
            if ym_short:
                parts.append(ym_short)
            label = "·".join(parts)

            if r["resolved_lon"] and r["resolved_lat"]:
                markers_data.append({
                    "pnu": pnu,
                    "lat": r["resolved_lat"],
                    "lon": r["resolved_lon"],
                    "color": jc,
                    "is_selected": is_sel,
                    "html": html,
                    "label": label,
                })
            geom = geom_map.get(pnu)
            if geom:
                coords = geom["coordinates"]
                if geom["type"] == "Polygon":
                    rings = [coords[0]]
                else:
                    rings = [poly[0] for poly in coords]
                polygons_data.append({
                    "pnu": pnu, "coords": rings,
                    "color": jc, "is_selected": is_sel, "html": html,
                })

        # 양방향 네이버 지도 컴포넌트 (declare_component) — 클릭 이벤트 수신
        # recenter: 검색이 새로 일어났을 때만 True (지도 중심 재설정)
        do_recenter = bool(st.session_state.get("_of_recenter_pending"))
        if do_recenter:
            st.session_state._of_recenter_pending = False
        map_event = of_naver_map(
            client_id=NAVER_MAP_CLIENT_ID,
            center=center, zoom=zoom,
            markers=markers_data, polygons=polygons_data,
            road_lines=result.get("road_lines"),
            sel_color=SELECTED_COLOR,
            zoom_label_threshold=15,
            recenter=do_recenter,
            key="of_naver_map",
        )
        # 지도 클릭 → PNU 동기화 (양방향)
        if isinstance(map_event, dict) and map_event.get("type") == "pnu_click":
            clicked_pnu = map_event.get("pnu")
            last_click_ts = st.session_state.get("_of_last_map_click_ts")
            this_ts = map_event.get("ts")
            if clicked_pnu and this_ts != last_click_ts:
                st.session_state._of_last_map_click_ts = this_ts
                if clicked_pnu != st.session_state.get("selected_pnu"):
                    st.session_state.selected_pnu = clicked_pnu
                    st.session_state.last_table_rows = []
                    st.session_state._dialog_shown_for = None  # dialog 재오픈
                    st.rerun()

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
            "🔴 선택된 필지  ·  호버하면 필지가 지목 색으로 강조  ·  "
            "줌 15+ 마커 옆에 평·평단가·년월 라벨  ·  "
            "✅ 지도↔표 양방향 동기화 작동"
        )

    # 양방향: 지도 클릭으로 selected_pnu가 변경됐다면 위쪽 of_naver_map 호출이
    # 이미 st.rerun을 트리거함. 여기서는 placeholder만 둠 (구 흐름 호환).
    new_pnu_from_map = None

    # ===== 표 =====
    with col_table:
        # 모바일 드로어가 이 column을 식별할 수 있도록 anchor
        st.markdown(
            '<div id="of-tbl-anchor" style="height:1px;"></div>',
            unsafe_allow_html=True,
        )
        st.subheader("📋 거래 목록")
        # 시군구 라벨 매핑
        SIGG_LABEL = {"41461": "처인", "41463": "기흥", "41465": "수지"}
        df_rows = []
        for d, r in results:
            row = {}
            if result["road_lines"]:
                row["거리(m)"] = int(d) if d is not None else None
            row["구"] = SIGG_LABEL.get(str(r["sigg_cd"]), "—")
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
            row["그룹"] = r.get("share_group", "")
            # 시세 이상치 라벨 (high_outlier → '🔥고평가', low_outlier → '❄️저평가')
            pa = r.get("price_anomaly")
            row["이상치"] = (
                "🔥 고평가" if pa == "high_outlier"
                else "❄️ 저평가" if pa == "low_outlier"
                else ""
            )
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
            "행 클릭 시 거래 상세 팝업 + 지도 확대"
        )

        df_display = df.head(500).copy().reset_index(drop=True)

        # 자체 표 컴포넌트 호출 (헤더 정렬·행 선택 모두 컴포넌트 내부 state)
        # 컬럼 정의 — type 추론은 컴포넌트가 자동, 명시도 가능
        table_columns = []
        for col in df_display.columns:
            is_num = col in (
                "거리(m)", "면적(㎡)", "면적(평)", "금액(만원)",
                "평단가(만원/평)",
            )
            table_columns.append({
                "name": col,
                "type": "number" if is_num else "string",
                "visible": (col != "PNU"),   # PNU는 데이터에는 있고 헤더에서 숨김
            })

        # rows를 dict 리스트로 (JSON 직렬화)
        table_rows = []
        for _, row in df_display.iterrows():
            d = {}
            for col in df_display.columns:
                v = row[col]
                # NaN → None, numpy int/float → python
                if pd.isna(v):
                    d[col] = None
                elif hasattr(v, "item"):
                    d[col] = v.item()
                else:
                    d[col] = v
            table_rows.append(d)

        # 정렬 초기값 — 검색 sort_by 기반 (헤더 한 번 누르면 그쪽 정렬됨)
        sort_by = result.get("sort_by") or "deal_ymd"
        sort_order = (result.get("sort_order") or "desc").lower()
        col_map = {
            "deal_ymd": "시기",
            "deal_amount": "금액(만원)",
            "area_m2": "면적(㎡)",
            "unit_per_pyeong": "평단가(만원/평)",
        }
        initial_sort_col = col_map.get(sort_by)
        initial_sort_dir = "asc" if sort_order == "asc" else "desc"

        table_event = of_trades_table(
            rows=table_rows,
            columns=table_columns,
            selected_pnu=prev_selected_pnu,
            initial_sort_col=initial_sort_col,
            initial_sort_dir=initial_sort_dir,
            key="of_trades_table",
        )

        if len(results) > 500:
            st.caption(
                f"※ 표에는 최대 500건만. 전체 {len(results):,}건은 엑셀로."
            )

    # 표 행 클릭 → PNU 동기화 (양방향)
    if isinstance(table_event, dict) and table_event.get("type") == "row_click":
        clicked_pnu = table_event.get("pnu")
        last_click_ts = st.session_state.get("_of_last_table_click_ts")
        this_ts = table_event.get("ts")
        if clicked_pnu and this_ts != last_click_ts:
            st.session_state._of_last_table_click_ts = this_ts
            if clicked_pnu != st.session_state.get("selected_pnu"):
                st.session_state.selected_pnu = clicked_pnu
                # dialog 다시 띄우기 위해 _dialog_shown_for 리셋
                st.session_state._dialog_shown_for = None
                st.rerun()
