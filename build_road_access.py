"""
build_road_access.py — 필지가 도로에 접면돼 있는지(맹지 여부) 자동 판정.

알고리즘:
  1. roads.geometry_json (LineString 좌표 리스트)을 lon/lat 격자에 인덱싱
  2. 각 필지 outer ring의 정점들에서 가까운 도로 후보(인접 격자만) 탐색
  3. 최단 도로 거리 ≤ ROAD_ACCESS_THRESHOLD_M 이면 has_road_access=1
  4. 결과를 parcels.has_road_access에 UPDATE

외부 데이터 불필요. 원삼면·백암면(geometry 100% 보유)만 대상.
"""

import json
import os
import sqlite3
import time
from collections import defaultdict
from math import cos, radians

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

ROAD_ACCESS_THRESHOLD_M = 10.0   # 필지 경계 ↔ 도로 중심선 거리
GRID_DEG = 0.005                 # ~500m 격자
def _load_all_prefix8():
    """region_prefix_cache.json에서 모든 emd의 prefix8 추출."""
    import json as _j
    p = os.path.join(HERE, "region_prefix_cache.json")
    if not os.path.exists(p):
        return ("41461340", "41461350")
    with open(p, encoding="utf-8") as f:
        d = _j.load(f)
    p8s = tuple(sorted({info["prefix8"] for info in d.get("emd_map", {}).values()}))
    return p8s if p8s else ("41461340", "41461350")


TARGET_PREFIX8 = _load_all_prefix8()

LAT_DEG_TO_M = 111049.0


def _seg_dist2_m(px, py, ax, ay, bx, by):
    """점(px,py)에서 선분 AB까지 거리² (m²). 좌표는 m 단위."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    qx, qy = ax + t * dx, ay + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2


def _grid_keys(lon, lat):
    return (int(lon / GRID_DEG), int(lat / GRID_DEG))


def load_road_index(conn):
    """roads.geometry_json → 격자 인덱스. 도로 선분 단위로 적재."""
    idx = defaultdict(list)  # (gx, gy) → [(seg_a_lon, seg_a_lat, seg_b_lon, seg_b_lat)]
    n_segs = 0
    for r in conn.execute("SELECT geometry_json FROM roads"):
        try:
            g = json.loads(r[0])
        except (TypeError, json.JSONDecodeError):
            continue
        coords = None
        if isinstance(g, dict):
            t = g.get("type")
            if t == "LineString":
                coords = g.get("coordinates")
            elif t == "MultiLineString":
                # 평탄화
                for line in g.get("coordinates", []):
                    _index_line(idx, line)
                    n_segs += max(0, len(line) - 1)
                continue
        elif isinstance(g, list):
            # 좌표 리스트 형태 (현 DB)
            if g and isinstance(g[0], list) and len(g[0]) == 2 \
                    and isinstance(g[0][0], (int, float)):
                coords = g
            elif g and isinstance(g[0], list) and isinstance(g[0][0], list):
                # 다중 라인
                for line in g:
                    _index_line(idx, line)
                    n_segs += max(0, len(line) - 1)
                continue
        if coords:
            _index_line(idx, coords)
            n_segs += max(0, len(coords) - 1)
    return idx, n_segs


def _index_line(idx, coords):
    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        lon_min = min(a[0], b[0])
        lon_max = max(a[0], b[0])
        lat_min = min(a[1], b[1])
        lat_max = max(a[1], b[1])
        gx0, gy0 = _grid_keys(lon_min, lat_min)
        gx1, gy1 = _grid_keys(lon_max, lat_max)
        seg = (a[0], a[1], b[0], b[1])
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                idx[(gx, gy)].append(seg)


def min_dist_to_roads_m(p_lon, p_lat, road_idx):
    """점 (lon,lat)에서 가장 가까운 도로 선분까지 거리(m).
    같은 격자 + 인접 8격자 후보만 검사."""
    gx, gy = _grid_keys(p_lon, p_lat)
    lon_m = LAT_DEG_TO_M * cos(radians(p_lat))
    lat_m = LAT_DEG_TO_M
    px, py = p_lon * lon_m, p_lat * lat_m
    min_d2 = float("inf")
    seen_segs = set()  # 동일 선분 중복 격자에 들어가는 경우 1번만
    for ddx in (-1, 0, 1):
        for ddy in (-1, 0, 1):
            for seg in road_idx.get((gx + ddx, gy + ddy), ()):
                if seg in seen_segs:
                    continue
                seen_segs.add(seg)
                ax, ay = seg[0] * lon_m, seg[1] * lat_m
                bx, by = seg[2] * lon_m, seg[3] * lat_m
                d2 = _seg_dist2_m(px, py, ax, ay, bx, by)
                if d2 < min_d2:
                    min_d2 = d2
    return min_d2 ** 0.5


def parcel_min_dist_m(geom_json, road_idx):
    """필지 outer ring의 정점들 중 도로에 가장 가까운 점까지 거리(m).
    필지 경계 전체를 정점 단위로 sampling."""
    try:
        g = json.loads(geom_json)
    except (TypeError, json.JSONDecodeError):
        return float("inf")
    t = g.get("type")
    coords = g.get("coordinates")
    if t == "Polygon":
        polys = [coords]
    elif t == "MultiPolygon":
        polys = coords
    else:
        return float("inf")
    min_d = float("inf")
    for poly in polys:
        outer = poly[0] if poly else None
        if not outer:
            continue
        for pt in outer:
            d = min_dist_to_roads_m(pt[0], pt[1], road_idx)
            if d < min_d:
                min_d = d
                if min_d <= ROAD_ACCESS_THRESHOLD_M:
                    return min_d  # 이미 접면 확정 — 조기 종료
    return min_d


def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" build_road_access — 필지 도로 접면 자동 판정")
    print("=" * 78)
    print(f"   임계값: {ROAD_ACCESS_THRESHOLD_M}m  (필지 경계 ↔ 도로 중심선)")
    print(f"   대상 emd: {len(TARGET_PREFIX8)}개 (모든 등록 emd)")

    print("\n[1] roads 격자 인덱스 구축...")
    t0 = time.time()
    road_idx, n_segs = load_road_index(conn)
    print(f"   {n_segs:,} 선분  /  {len(road_idx):,} 격자 셀  "
          f"({time.time()-t0:.1f}초)")

    print("\n[2] 대상 필지 로드...")
    ph = ",".join("?" * len(TARGET_PREFIX8))
    rows = list(conn.execute(
        f"SELECT pnu, geometry_json FROM parcels "
        f"WHERE prefix8 IN ({ph}) AND geometry_json IS NOT NULL",
        TARGET_PREFIX8))
    print(f"   {len(rows):,}필지")

    print("\n[3] 도로 접면 판정 (필지마다)...")
    t0 = time.time()
    updates = []
    n_access = 0
    n_total = len(rows)
    log_every = max(1, n_total // 10)
    for i, (pnu, geom) in enumerate(rows, 1):
        d = parcel_min_dist_m(geom, road_idx)
        access = 1 if d <= ROAD_ACCESS_THRESHOLD_M else 0
        if access:
            n_access += 1
        updates.append((access, pnu))
        if i % log_every == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n_total - i) / rate if rate > 0 else 0
            print(f"   {i:>6,}/{n_total:,}  접면 {n_access:>5,}건 "
                  f"({n_access/i*100:.1f}%)  "
                  f"속도 {rate:.0f}건/초  ETA {eta:.0f}초")
    print(f"   완료 ({time.time()-t0:.1f}초)")

    print("\n[4] DB 업데이트...")
    conn.executemany(
        "UPDATE parcels SET has_road_access=? WHERE pnu=?",
        updates,
    )
    conn.commit()

    print("\n[5] 결과 분포 — 시군구별")
    print("-" * 78)
    sigg_name = {"41461": "처인구", "41463": "기흥구", "41465": "수지구"}
    for r in conn.execute(
        f"SELECT SUBSTR(prefix8,1,5) sgg, "
        f"SUM(CASE WHEN has_road_access=1 THEN 1 ELSE 0 END) access, "
        f"COUNT(*) total FROM parcels "
        f"WHERE prefix8 IN ({ph}) GROUP BY sgg ORDER BY 1",
        TARGET_PREFIX8
    ):
        pct = r[1] / r[2] * 100 if r[2] else 0
        name = sigg_name.get(r[0], r[0])
        print(f"   {name}: 접면 {r[1]:,}/{r[2]:,} ({pct:.1f}%)")

    print("\n[6] 지목별 접면율 (전 대상 합산)")
    print("-" * 78)
    for r in conn.execute(
        f"SELECT jimok, SUM(CASE WHEN has_road_access=1 THEN 1 ELSE 0 END) access, "
        f"COUNT(*) total FROM parcels "
        f"WHERE prefix8 IN ({ph}) GROUP BY jimok ORDER BY 3 DESC LIMIT 12",
        TARGET_PREFIX8
    ):
        pct = r[1] / r[2] * 100 if r[2] else 0
        print(f"   {r[0]:8s}: {r[1]:>6,}/{r[2]:>6,} ({pct:>5.1f}%)")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
