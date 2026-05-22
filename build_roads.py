"""
build_roads.py — 처인구 박스 도로 데이터를 trades.db 의 roads 테이블에 적재 +
'덕평로 반경 5km 임야 거래'로 거리 계산 동작 검증.

도로 데이터: V-World LT_L_MOCTLINK (표준노드링크 = 도로 링크 단위 라인스트링).
"""

import json
import os
import sqlite3
import statistics
import time
from collections import Counter
from math import cos, radians

import requests
from api_keys import VWORLD_KEY


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")


# =====================================================================
#  스키마
# =====================================================================
def init_roads_table(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS roads (
        link_id TEXT PRIMARY KEY,
        road_name TEXT,
        rd_rank_h TEXT,
        rd_type_h TEXT,
        max_spd INTEGER,
        geometry_json TEXT,
        min_lon REAL, max_lon REAL,
        min_lat REAL, max_lat REAL
    );
    CREATE INDEX IF NOT EXISTS idx_roads_name ON roads(road_name);
    CREATE INDEX IF NOT EXISTS idx_roads_rank ON roads(rd_rank_h);
    CREATE INDEX IF NOT EXISTS idx_roads_bbox ON roads(min_lon, max_lon, min_lat, max_lat);
    """)
    conn.commit()


# =====================================================================
#  V-World 도로 받기
# =====================================================================
def fetch_roads_in_box(bbox):
    rows = []
    page = 1
    page_total = None
    while True:
        r = requests.get(
            "https://api.vworld.kr/req/data",
            params={
                "service": "data", "request": "GetFeature",
                "data": "LT_L_MOCTLINK", "key": VWORLD_KEY,
                "geomFilter": f"BOX({bbox})",
                "page": str(page), "size": "1000",
                "geometry": "true", "format": "json",
            },
            timeout=60,
        )
        data = r.json()
        resp = data.get("response", {})
        if resp.get("status") != "OK":
            print(f"   page {page}: status={resp.get('status')}, 종료")
            break
        if page == 1:
            page_total = int(resp.get("page", {}).get("total", "1"))
        feats = (resp.get("result", {}).get("featureCollection", {}) or {}).get("features", [])
        if not feats:
            break
        for ft in feats:
            p = ft["properties"]
            g = ft.get("geometry") or {}
            coords = g.get("coordinates") or []
            if g.get("type") == "MultiLineString" and coords:
                coords = coords[0]
            if not coords or len(coords) < 2:
                continue
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            try:
                max_spd = int(p.get("max_spd") or 0)
            except (TypeError, ValueError):
                max_spd = 0
            rows.append((
                p["link_id"], p.get("road_name") or "",
                p.get("rd_rank_h") or "",
                p.get("rd_type_h") or "",
                max_spd,
                json.dumps(coords),
                min(lons), max(lons), min(lats), max(lats),
            ))
        print(f"   page {page}/{page_total}: 누적 {len(rows):,}")
        if page >= page_total:
            break
        page += 1
        time.sleep(0.1)
    return rows


# =====================================================================
#  거리 계산 (점 → 라인스트링)
# =====================================================================
def _seg_dist2(px, py, ax, ay, bx, by):
    """점 P → 세그먼트 AB 최단거리^2 (m² 단위, 평면)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def point_to_line_m(p_lon, p_lat, coords):
    """점(p_lon,p_lat) → 라인 coords 최단거리(m). WGS84 평면근사."""
    if not coords or len(coords) < 2:
        return float("inf")
    lon_m = 111049.0 * cos(radians(p_lat))
    lat_m = 111049.0
    px = p_lon * lon_m
    py = p_lat * lat_m
    min_d2 = float("inf")
    for i in range(len(coords) - 1):
        ax = coords[i][0] * lon_m
        ay = coords[i][1] * lat_m
        bx = coords[i + 1][0] * lon_m
        by = coords[i + 1][1] * lat_m
        d2 = _seg_dist2(px, py, ax, ay, bx, by)
        if d2 < min_d2:
            min_d2 = d2
    return min_d2 ** 0.5


# =====================================================================
#  검증 쿼리
# =====================================================================
def find_trades_near_road(conn, road_name, radius_m=5000, jimok=None):
    cur = conn.execute(
        "SELECT geometry_json, min_lon, max_lon, min_lat, max_lat "
        "FROM roads WHERE road_name = ?",
        (road_name,),
    )
    road_lines = []
    bbox = None
    for row in cur:
        coords = json.loads(row[0])
        road_lines.append(coords)
        if bbox is None:
            bbox = [row[1], row[2], row[3], row[4]]
        else:
            bbox[0] = min(bbox[0], row[1])
            bbox[1] = max(bbox[1], row[2])
            bbox[2] = min(bbox[2], row[3])
            bbox[3] = max(bbox[3], row[4])
    if not road_lines:
        return [], 0, 0

    # 반경 박스로 후보 좁히기 (인덱스 활용)
    rad_deg = radius_m / 111049.0
    qmin_lon = bbox[0] - rad_deg
    qmax_lon = bbox[1] + rad_deg
    qmin_lat = bbox[2] - rad_deg
    qmax_lat = bbox[3] + rad_deg

    where = [
        "resolved_lon BETWEEN ? AND ?",
        "resolved_lat BETWEEN ? AND ?",
        "resolved_pnu IS NOT NULL",
    ]
    params = [qmin_lon, qmax_lon, qmin_lat, qmax_lat]
    if jimok:
        where.append("jimok = ?")
        params.append(jimok)

    sql = (
        "SELECT id, umd_name, jimok, area_m2, deal_amount, deal_ymd, "
        "resolved_pnu, resolved_jibun, resolved_lon, resolved_lat, "
        "unit_per_pyeong, match_confidence "
        "FROM trades WHERE " + " AND ".join(where)
    )
    candidates = list(conn.execute(sql, params))

    near = []
    for r in candidates:
        plon, plat = r["resolved_lon"], r["resolved_lat"]
        if plon is None or plat is None:
            continue
        d = min(point_to_line_m(plon, plat, line) for line in road_lines)
        if d <= radius_m:
            near.append((d, r))

    near.sort(key=lambda x: x[0])
    return near, len(candidates), len(road_lines)


# =====================================================================
#  메인
# =====================================================================
def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_roads_table(conn)

    n_roads = conn.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
    if n_roads > 0:
        print(f"[1] roads 테이블 이미 {n_roads:,}건 적재됨, 스킵\n")
    else:
        # 처인구 전체 박스 + 5km 마진
        b = conn.execute(
            "SELECT MIN(lon), MAX(lon), MIN(lat), MAX(lat) FROM parcels"
        ).fetchone()
        margin = 0.05
        bbox = f"{b[0]-margin:.4f},{b[2]-margin:.4f},{b[1]+margin:.4f},{b[3]+margin:.4f}"
        print(f"[1] V-World 도로 데이터 받기  (geometry=true, 좌표까지)")
        print(f"   처인구 박스 ±5km : {bbox}")
        t0 = time.time()
        rows = fetch_roads_in_box(bbox)
        print(f"   총 {len(rows):,}개 도로 링크 ({time.time()-t0:.1f}초)")
        conn.executemany(
            "INSERT OR REPLACE INTO roads "
            "(link_id, road_name, rd_rank_h, rd_type_h, max_spd, "
            "geometry_json, min_lon, max_lon, min_lat, max_lat) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        print(f"   roads 테이블 저장 완료\n")

    # 적재 요약
    print("[2] 도로 데이터 요약")
    print("-" * 80)
    cur = conn.execute("SELECT COUNT(*) FROM roads")
    n_roads = cur.fetchone()[0]
    print(f"   총 도로 링크: {n_roads:,}건")
    print(f"\n   도로 위계별:")
    for row in conn.execute(
        "SELECT rd_rank_h, COUNT(*) FROM roads GROUP BY rd_rank_h "
        "ORDER BY COUNT(*) DESC"
    ):
        print(f"     {row[1]:>6,}  {row[0]}")

    # 검증: 덕평로 반경 5km 임야
    print("\n" + "=" * 80)
    print(" [3] 검증 — '원삼면 인근, 덕평로 반경 5km 임야'")
    print("=" * 80)

    near, n_bbox, n_road_links = find_trades_near_road(
        conn, road_name="덕평로", radius_m=5000, jimok="임야"
    )
    print(f"\n   덕평로 도로 링크: {n_road_links}개")
    print(f"   덕평로 통합 박스 ±5km 안 임야 매칭거래(후보): {n_bbox}건")
    print(f"   실제 반경 5km 안: {len(near)}건")

    # 거리 분포
    print(f"\n   거리 분포:")
    for lo, hi in [(0, 500), (500, 1000), (1000, 2000), (2000, 5000)]:
        cnt = sum(1 for d, _ in near if lo <= d < hi)
        print(f"     {lo:>5}~{hi:>5}m : {cnt:>4}건")

    # high·단독매매 평단가 (시세 노이즈 적게)
    high = [(d, r) for d, r in near if r["match_confidence"] == "high"]
    pnu_count = Counter(r["resolved_pnu"] for _, r in high)
    solo = [(d, r) for d, r in high if pnu_count[r["resolved_pnu"]] == 1]
    units = [r["unit_per_pyeong"] for _, r in solo if r["unit_per_pyeong"]]
    if units:
        print(f"\n   high+단독매매 {len(solo)}건의 평단가 (시세 추정):")
        print(f"     중앙값 {statistics.median(units):>7,.0f}만원/평")
        print(f"     평균   {sum(units)/len(units):>7,.0f}만원/평")
        print(f"     범위   {min(units):,.0f} ~ {max(units):,.0f}")

    # 가까운 거 10건 미리보기
    print(f"\n   가장 가까운 임야 거래 10건:")
    print(f"   {'거리':>6s}  {'동·리':18s}  {'지번':14s}  {'면적':>8s}  "
          f"{'금액':>10s}  {'평단가':>9s}  시기")
    for d, r in near[:10]:
        unit = r["unit_per_pyeong"] or 0
        jibun = r["resolved_jibun"] or "?"
        print(f"   {d:>5.0f}m  {r['umd_name'][:18]:18s}  {jibun[:14]:14s}  "
              f"{r['area_m2']:>7,.0f}㎡  {r['deal_amount']:>9,}만원  "
              f"{unit:>6,.0f}만원/평  {r['deal_ymd']}")

    conn.close()
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
