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
                    lookup_reference_parcel, fill_cond_from_reference)


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
#  Streamlit UI — OneFamily 실거래가 (찐파랑 컨셉)
# =====================================================================
BRAND_NAVY = "#2563EB"       # 산뜻한 모던 파랑 (Tailwind blue-600)
BRAND_NAVY_DEEP = "#1D4ED8"  # 강조 톤 (blue-700)
BRAND_NAVY_LIGHT = "#EFF6FF" # 배경 옅은 파랑 (blue-50)

st.set_page_config(
    page_title="OneFamily 실거래가",
    page_icon="🏛️", layout="wide",
    initial_sidebar_state="expanded",
)

# 글로벌 CSS — 모던 + 찐파랑
st.markdown(f"""
<style>
  :root {{
    --navy: {BRAND_NAVY};
    --navy-deep: {BRAND_NAVY_DEEP};
    --navy-light: {BRAND_NAVY_LIGHT};
  }}
  /* 간결한 헤더 */
  .of-brand {{
    display: flex; align-items: baseline; gap: 10px;
    margin-bottom: 2px;
  }}
  .of-brand .of-logo {{
    font-size: 22px; font-weight: 700; color: {BRAND_NAVY_DEEP};
    letter-spacing: -0.02em;
  }}
  .of-brand .of-logo-accent {{ color: {BRAND_NAVY}; }}
  .of-brand .of-badge {{
    display: inline-block; background: {BRAND_NAVY_LIGHT};
    color: {BRAND_NAVY_DEEP}; padding: 2px 8px; border-radius: 6px;
    font-size: 11px; font-weight: 500; letter-spacing: 0.02em;
    border: 1px solid #dbeafe;
  }}
  .of-brand-sub {{
    color: #64748b; font-size: 13px; margin-bottom: 18px;
  }}
  /* GPT 스타일 응답 카드 */
  .of-gpt-card {{
    background: white; border: 1px solid #e2e8f0;
    border-left: 3px solid {BRAND_NAVY};
    padding: 18px 20px; border-radius: 12px; margin: 18px 0;
    font-size: 14.5px; line-height: 1.65; color: #1e293b;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  }}
  .of-gpt-card .of-gpt-icon {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 26px; height: 26px; background: {BRAND_NAVY}; color: white;
    border-radius: 7px; font-size: 11px; font-weight: 700;
    margin-right: 9px; vertical-align: middle;
    letter-spacing: 0.02em;
  }}
  .of-gpt-card .of-gpt-title {{
    font-weight: 600; color: #475569; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.08em;
    margin-bottom: 10px; display: flex; align-items: center;
  }}
  /* 모던 메트릭 */
  div[data-testid="stMetric"] {{
    background: white; padding: 16px 18px; border-radius: 12px;
    border: 1px solid #e2e8f0;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
  }}
  div[data-testid="stMetric"] > div:first-child label {{
    font-size: 11.5px !important; color: #64748b !important;
    font-weight: 500 !important; text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  div[data-testid="stMetric"] [data-testid="stMetricValue"] {{
    color: {BRAND_NAVY_DEEP}; font-weight: 700; font-size: 22px;
  }}
  /* primary 버튼 */
  div.stButton > button[kind="primary"] {{
    background: {BRAND_NAVY}; border: none;
    font-weight: 600; letter-spacing: 0.01em;
    box-shadow: 0 1px 3px rgba(37, 99, 235, 0.25);
    transition: all 0.15s ease;
  }}
  div.stButton > button[kind="primary"]:hover {{
    background: {BRAND_NAVY_DEEP};
    box-shadow: 0 2px 6px rgba(37, 99, 235, 0.35);
    transform: translateY(-1px);
  }}
  /* 사이드바 강조 박스 */
  .of-scope-box {{
    background: {BRAND_NAVY_LIGHT}; border-radius: 12px;
    padding: 14px 14px; font-size: 13px; line-height: 1.55;
    color: {BRAND_NAVY_DEEP}; margin-bottom: 10px;
    border: 1px solid #dbeafe;
  }}
  .of-scope-box .of-scope-title {{
    font-weight: 700; font-size: 11px; letter-spacing: 0.06em;
    color: {BRAND_NAVY}; margin-bottom: 8px; text-transform: uppercase;
  }}
  .of-scope-box .of-scope-num {{
    font-size: 17px; font-weight: 700; color: {BRAND_NAVY_DEEP};
  }}
  /* 검색 입력 박스 */
  .stTextInput > div > div > input {{
    font-size: 15px; padding: 12px 16px;
    border-radius: 10px; border: 1.5px solid #e2e8f0;
    transition: all 0.15s ease;
  }}
  .stTextInput > div > div > input:focus {{
    border-color: {BRAND_NAVY};
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
  }}
  /* 배경 톤 살짝 */
  .main .block-container {{
    padding-top: 2.4rem; padding-bottom: 4rem;
  }}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown(
        f"<div style='font-size:20px;font-weight:700;color:{BRAND_NAVY_DEEP};"
        f"letter-spacing:-0.02em;'>🏛️ OneFamily</div>"
        f"<div style='font-size:12px;color:#6b7280;margin-top:2px;'>실거래가 분석 도구</div>",
        unsafe_allow_html=True,
    )
    st.divider()
    st.markdown(
        f"""
        <div class="of-scope-box">
          <div class="of-scope-title">검색 대상 DB</div>
          <div style="margin-bottom:4px;">📍 <b>용인 처인구 백암면 · 원삼면</b></div>
          <div style="font-size:12px;color:#475569;">
            기간: 2021-01-06 ~ 2026-05-20<br>
            토지 거래 <span class="of-scope-num">7,442</span>건
            (매칭 <b>2,232</b>건)<br>
            필지 <span class="of-scope-num">68,393</span>개<br>
            데이터 출처: 국토부 · V-World · SRTM 30m
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="of-scope-box" style="background:#f8fafc;">
          <div class="of-scope-title" style="color:#475569;">활용 가능 필터</div>
          <div style="font-size:12px;color:#475569;line-height:1.7;">
            ✓ 지목 · 면적 · 기간 · 금액 · 평단가<br>
            ✓ 도로 반경 · 다중 도로 OR<br>
            ✓ 해발 · 경사 · 맹지 · 공유지분<br>
            ✓ 'X와 비슷한 조건' 참조 검색
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()
    include_road = st.toggle("도로 지목 포함", value=False,
                              help="국가·공공 수용 거래가 많아 기본 제외")
    st.divider()
    with st.expander("⚠️ 활용 시 유의", expanded=False):
        st.caption(
            "• **'대'**(대지) 거래는 토지+건물 합산일 수 있어 시세 왜곡 가능\n\n"
            "• 별표 지번 복원 못 한 거래(low)는 결과에서 제외\n\n"
            "• 본 도구는 참고용. 실거래 시 등기부등본·현장 확인 필수"
        )
    with st.expander("🐛 디버그", expanded=False):
        st.caption(f"selected_pnu: `{st.session_state.get('selected_pnu')}`")
        st.caption(f"last_map_click_sig: `{st.session_state.get('last_map_click_sig')}`")
        st.caption(f"last_table_rows: `{st.session_state.get('last_table_rows')}`")

# 간결한 헤더 — 로고 + 부제만
st.markdown(
    f"""
    <div class="of-brand">
      <div class="of-logo">One<span class="of-logo-accent">Family</span> 실거래가</div>
      <span class="of-badge">용인 처인구 · 백암 / 원삼 테스트</span>
    </div>
    <div class="of-brand-sub">자연어 한 줄로 토지 실거래를 분석합니다</div>
    <div style="font-size:12.5px;color:#64748b;margin-bottom:12px;line-height:1.7;">
      예시 · "원삼면 임야 평당 100 미만 최근 1년"
      &nbsp;·&nbsp; "지방도318 반경 5km 1억 이하"
      &nbsp;·&nbsp; "두창리 957-5와 비슷한 조건"
    </div>
    """,
    unsafe_allow_html=True,
)

query = st.text_input(
    "질의", placeholder="자연어로 원하는 조건을 한 줄 입력하세요 (예: 두창리 957-5와 비슷한 조건의 실거래가)",
    label_visibility="collapsed",
)
go = st.button("🔍 검색 시작", type="primary", use_container_width=True)

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
    # 검색이 새로 일어났을 때만 지도 중심 재설정 (지도 컴포넌트가 recenter=True 받음)
    st.session_state._of_recenter_pending = True
    st.session_state._of_last_map_click_ts = None
    st.session_state.map_key = f"map_{datetime.now().timestamp()}"

# 결과 표시
if "result" in st.session_state:
    result = st.session_state.result
    cond = result["cond"]
    results = result["results"]

    if result["out_of_range"]:
        st.warning("⚠️ 테스트 버전은 **백암면 · 원삼면** 만 검색 가능. "
                   "그 외 지역은 무시.")

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

    with st.expander("🔧 자연어 파싱 결과 (원본 JSON)"):
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

    # 시세 요약
    st.subheader("💰 시세 요약")
    solo = [(d, r) for d, r in results
            if r["match_confidence"] == "high" and r.get("share_group") == "단독"]
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

    # 선택된 PNU가 있으면 그 필지의 모든 거래 원본 데이터 카드 표시
    if prev_selected_pnu:
        sel_conn = get_conn()
        sel_trades = list(sel_conn.execute(
            """
            SELECT id, sigg_cd, umd_name, jimok, area_m2, jibun_masked, is_san,
                   deal_amount, deal_year, deal_month, deal_day, deal_ymd,
                   land_use, dealing_gbn,
                   match_confidence, resolved_pnu, resolved_jibun,
                   resolved_area_m2, resolved_lon, resolved_lat, resolved_jiga,
                   unit_per_pyeong, candidates_count, share_label
            FROM trades WHERE resolved_pnu = ?
            ORDER BY deal_ymd DESC
            """,
            (prev_selected_pnu,),
        ))
        sel_parcel = sel_conn.execute(
            """
            SELECT pnu, jibun, jimok, area_m2, jiga, addr,
                   elevation_m, slope_deg, has_road_access,
                   shape_type, zone_type, prefix8
            FROM parcels WHERE pnu = ?
            """,
            (prev_selected_pnu,),
        ).fetchone()

        n_trades = len(sel_trades)
        with st.container(border=True):
            head_cols = st.columns([3, 1])
            with head_cols[0]:
                if sel_parcel:
                    py = (sel_parcel["area_m2"] / PYEONG_PER_M2
                          if sel_parcel["area_m2"] else 0)
                    st.markdown(
                        f"### 📄 선택 필지: **{sel_parcel['jibun']}** "
                        f"({sel_parcel['jimok']}, "
                        f"{int(sel_parcel['area_m2']):,}㎡ · {py:,.0f}평)"
                    )
                    addr = sel_parcel["addr"] or ""
                    sub_bits = []
                    if addr: sub_bits.append(addr)
                    sub_bits.append(f"PNU `{prev_selected_pnu}`")
                    sub_bits.append(f"거래 **{n_trades}건** 매칭됨")
                    st.caption(" · ".join(sub_bits))
                else:
                    st.markdown(f"### 📄 선택 PNU: `{prev_selected_pnu}`")
            with head_cols[1]:
                if st.button("✕ 선택 해제", use_container_width=True,
                             key="clear_selection"):
                    st.session_state.selected_pnu = None
                    st.session_state.last_table_rows = []
                    st.rerun()

            # 필지 기본 정보 행
            if sel_parcel:
                info_cols = st.columns(6)
                info_cols[0].metric(
                    "공시지가",
                    f"{int(sel_parcel['jiga']):,}원/㎡" if sel_parcel['jiga'] else "—",
                )
                info_cols[1].metric(
                    "해발",
                    f"{sel_parcel['elevation_m']:.0f}m"
                    if sel_parcel["elevation_m"] is not None else "—",
                )
                info_cols[2].metric(
                    "경사",
                    f"{sel_parcel['slope_deg']:.1f}°"
                    if sel_parcel["slope_deg"] is not None else "—",
                )
                info_cols[3].metric(
                    "도로 접면",
                    "접면" if sel_parcel["has_road_access"] == 1
                    else ("맹지" if sel_parcel["has_road_access"] == 0 else "—"),
                )
                info_cols[4].metric("형상", sel_parcel["shape_type"] or "—")
                info_cols[5].metric("용도지역", sel_parcel["zone_type"] or "—")

            # 거래 원본 데이터 (API 그대로)
            st.markdown(
                f"<div style='margin-top:14px;font-weight:600;"
                f"color:{BRAND_NAVY_DEEP};font-size:13.5px;'>"
                f"📋 국토부 토지매매 실거래 원본 데이터</div>",
                unsafe_allow_html=True,
            )
            for i, t in enumerate(sel_trades):
                with st.expander(
                    f"#{i+1}  {t['deal_ymd'][:10]}  "
                    f"{t['deal_amount']:,}만원  "
                    f"{t['area_m2']:,.0f}㎡  "
                    f"mask=`{t['jibun_masked']}`  "
                    f"[{t['match_confidence']}]",
                    expanded=(i == 0),
                ):
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
                        }
                        for k, v in of_data.items():
                            st.markdown(
                                f"<div style='font-size:13px;line-height:1.7;'>"
                                f"<span style='color:#64748b;'>{k}</span>: "
                                f"<b>{v}</b></div>",
                                unsafe_allow_html=True,
                            )
        st.divider()

    col_map, col_table = st.columns([1, 1])

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
            row["그룹"] = r.get("share_group", "")
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
