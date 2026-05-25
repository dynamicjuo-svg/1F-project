"""
build_road_frontage.py — 필지 outer ring 변(side) 단위 도로 접면 분석.

벨류맵 흡수 #2: 접도 길이 + 접도 형태 + 접도 각도.

알고리즘:
  1. roads.geometry_json → 격자 인덱스 (a→b 선분 단위)
  2. 필지 outer ring의 각 변에 대해:
     - 변 중점에서 가장 가까운 도로 선분 찾기 (≤ROAD_THRESHOLD_M)
     - 가까운 경우: 변 단위벡터와 도로 단위벡터 사이 각도(0~90°)
  3. 산출:
     - road_frontage_m       (접도 변 길이 합)
     - road_frontage_ratio   (frontage / perimeter)
     - road_n_sides          (접도 변 수)
     - is_corner_lot         (≥2면 접도)
     - road_frontage_angle_deg (변·도로 가중평균 각도, 0=평행=베스트)

대상: TARGET_PREFIX8 (region_prefix_cache.json에서 자동) 중 geometry 있는 필지.
"""

import json
import math
import os
import sqlite3
import time
from collections import Counter, defaultdict
from math import cos, radians

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

ROAD_THRESHOLD_M = 10.0
GRID_DEG = 0.005
PARALLEL_ANGLE_MAX_DEG = 30.0   # 변·도로 각도 ≤30° 이면 진짜 접면 (옵션 필터용)
LAT_DEG_TO_M = 111049.0


def _load_all_prefix8():
    p = os.path.join(HERE, "region_prefix_cache.json")
    if not os.path.exists(p):
        return ("41461340", "41461350")
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    p8s = tuple(sorted({info["prefix8"] for info in d.get("emd_map", {}).values()}))
    return p8s if p8s else ("41461340", "41461350")


TARGET_PREFIX8 = _load_all_prefix8()


# =====================================================================
#  Road grid index (선분 단위, lon/lat → m 변환 정보 같이 보관)
# =====================================================================
def _grid_keys(lon, lat):
    return (int(lon / GRID_DEG), int(lat / GRID_DEG))


def load_road_index(conn):
    """roads.geometry_json → 격자 인덱스.
    각 셀에 [(a_lon, a_lat, b_lon, b_lat)] 보관."""
    idx = defaultdict(list)
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
                for line in g.get("coordinates", []):
                    _index_line(idx, line)
                    n_segs += max(0, len(line) - 1)
                continue
        elif isinstance(g, list):
            if g and isinstance(g[0], list) and len(g[0]) == 2 \
                    and isinstance(g[0][0], (int, float)):
                coords = g
            elif g and isinstance(g[0], list) and isinstance(g[0][0], list):
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
        a = coords[i]; b = coords[i + 1]
        lon_min = min(a[0], b[0]); lon_max = max(a[0], b[0])
        lat_min = min(a[1], b[1]); lat_max = max(a[1], b[1])
        gx0, gy0 = _grid_keys(lon_min, lat_min)
        gx1, gy1 = _grid_keys(lon_max, lat_max)
        seg = (a[0], a[1], b[0], b[1])
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                idx[(gx, gy)].append(seg)


# =====================================================================
#  점 → 도로 선분 최단거리 + 그 도로 선분의 단위벡터
# =====================================================================
def _seg_dist2_with_vec(px, py, ax, ay, bx, by):
    """점(px,py) → 선분 AB 최단거리² 및 AB 길이 반환.
    좌표는 m 평면."""
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0:
        return (px - ax) ** 2 + (py - ay) ** 2, 0.0, 0.0, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t = max(0.0, min(1.0, t))
    qx, qy = ax + t * dx, ay + t * dy
    d2 = (px - qx) ** 2 + (py - qy) ** 2
    return d2, dx, dy, math.sqrt(seg_len2)


def nearest_road_seg(p_lon, p_lat, road_idx):
    """점에서 가장 가까운 도로 선분의 거리(m)와 그 선분 단위벡터(m 평면) 반환.

    Return: (dist_m, road_ux_m, road_uy_m)  — 도로 없거나 너무 멀면 (inf, 0, 0)
    """
    gx, gy = _grid_keys(p_lon, p_lat)
    lon_m = LAT_DEG_TO_M * cos(radians(p_lat))
    lat_m = LAT_DEG_TO_M
    px, py = p_lon * lon_m, p_lat * lat_m
    min_d2 = float("inf")
    best_ux = best_uy = 0.0
    seen = set()
    for ddx in (-1, 0, 1):
        for ddy in (-1, 0, 1):
            for seg in road_idx.get((gx + ddx, gy + ddy), ()):
                if seg in seen:
                    continue
                seen.add(seg)
                ax, ay = seg[0] * lon_m, seg[1] * lat_m
                bx, by = seg[2] * lon_m, seg[3] * lat_m
                d2, dx, dy, seg_len = _seg_dist2_with_vec(
                    px, py, ax, ay, bx, by)
                if d2 < min_d2:
                    min_d2 = d2
                    if seg_len > 0:
                        best_ux = dx / seg_len
                        best_uy = dy / seg_len
                    else:
                        best_ux = best_uy = 0.0
    return math.sqrt(min_d2), best_ux, best_uy


# =====================================================================
#  Outer ring + frontage 계산
# =====================================================================
def _outer_ring(geom):
    if not isinstance(geom, dict):
        return None
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t == "Polygon":
        return coords[0] if coords else None
    elif t == "MultiPolygon":
        if not coords:
            return None
        biggest, biggest_a = None, -1
        for poly in coords:
            if not poly:
                continue
            ring = poly[0]
            s = 0.0
            n = len(ring)
            for i in range(n):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                s += x1 * y2 - x2 * y1
            a = abs(s) / 2.0
            if a > biggest_a:
                biggest_a, biggest = a, ring
        return biggest
    return None


SAME_DIR_DOT_THRESHOLD = 0.85   # 두 도로 단위벡터 |dot| ≥ 0.85 (~32° 내) 이면 같은 그룹


def frontage_features(geom_json, road_idx):
    """outer ring 변마다 도로 접면 분석 + 연속 변 그룹화.

    Step1: 각 변에 대해 frontage 여부 + 도로 단위벡터(rx,ry) 저장
    Step2: 변을 순환(circular) 순서로 보며, 인접 frontage 변들의 도로 방향이
           유사하면(|dot|≥0.85) 같은 그룹으로 묶음
    Step3: 그룹 수 = n_sides (1면/2면/3면 접도)

    Return: dict 또는 None
      - frontage_m, frontage_ratio, n_sides(=그룹 수), is_corner, frontage_angle_deg
    """
    try:
        g = json.loads(geom_json) if isinstance(geom_json, str) else geom_json
    except (TypeError, json.JSONDecodeError):
        return None
    ring = _outer_ring(g)
    if not ring or len(ring) < 3:
        return None
    lat0 = sum(p[1] for p in ring) / len(ring)
    lon_m = LAT_DEG_TO_M * cos(radians(lat0))
    lat_m = LAT_DEG_TO_M
    pts_m = [(p[0] * lon_m, p[1] * lat_m) for p in ring]
    n = len(pts_m)
    if n < 3:
        return None

    # Step 1: 변별 frontage 여부 + 도로 단위벡터 수집
    sides = []  # 리스트 of dict
    perimeter = 0.0
    for i in range(n):
        ax, ay = pts_m[i]
        bx, by = pts_m[(i + 1) % n]
        side_len = math.hypot(bx - ax, by - ay)
        if side_len == 0:
            sides.append(None)
            continue
        perimeter += side_len
        mid_lon = (ring[i][0] + ring[(i + 1) % n][0]) / 2
        mid_lat = (ring[i][1] + ring[(i + 1) % n][1]) / 2
        d, rx, ry = nearest_road_seg(mid_lon, mid_lat, road_idx)
        if d <= ROAD_THRESHOLD_M and (rx != 0 or ry != 0):
            ex = (bx - ax) / side_len
            ey = (by - ay) / side_len
            cos_a = abs(ex * rx + ey * ry)
            cos_a = max(0.0, min(1.0, cos_a))
            angle_deg = math.degrees(math.acos(cos_a))
            if angle_deg <= 60.0:
                sides.append({
                    "len": side_len, "angle": angle_deg, "rx": rx, "ry": ry,
                    "front": True,
                })
                continue
        sides.append({"len": side_len, "front": False, "rx": 0, "ry": 0})

    if perimeter <= 0:
        return None

    # Step 2: 연속 frontage 변 그룹화 (circular)
    # 시작점을 non-frontage 변으로 잡아야 그룹 경계가 명확
    valid_sides = [s for s in sides if s is not None]
    if not valid_sides:
        return {
            "frontage_m": 0.0, "frontage_ratio": 0.0,
            "n_sides": 0, "is_corner": 0, "angle_deg": None,
        }

    # circular shift: 첫 non-frontage 인덱스 찾아 거기서 시작
    nv = len(valid_sides)
    start = 0
    for k in range(nv):
        if not valid_sides[k]["front"]:
            start = k
            break
    # 만약 전체가 frontage이면 그냥 0부터 시작 (그룹 1개로 묶일 것)

    n_groups = 0
    frontage_len = 0.0
    angle_weighted_sum = 0.0
    in_group = False
    prev_rx = prev_ry = 0.0
    for k in range(nv):
        s = valid_sides[(start + k) % nv]
        if not s["front"]:
            in_group = False
            continue
        frontage_len += s["len"]
        angle_weighted_sum += s["angle"] * s["len"]
        if not in_group:
            n_groups += 1
            in_group = True
        else:
            # 직전 변과 도로 방향 유사도 체크
            dot = abs(s["rx"] * prev_rx + s["ry"] * prev_ry)
            if dot < SAME_DIR_DOT_THRESHOLD:
                n_groups += 1   # 방향 꺾이면 새 그룹
        prev_rx, prev_ry = s["rx"], s["ry"]

    ratio = frontage_len / perimeter
    avg_angle = (angle_weighted_sum / frontage_len) if frontage_len > 0 else None
    return {
        "frontage_m": round(frontage_len, 2),
        "frontage_ratio": round(ratio, 4),
        "n_sides": n_groups,
        "is_corner": 1 if n_groups >= 2 else 0,
        "angle_deg": round(avg_angle, 1) if avg_angle is not None else None,
    }


# =====================================================================
#  Main
# =====================================================================
def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" build_road_frontage — 변 단위 접도 분석 (길이/형태/각도)")
    print("=" * 78)
    print(f"   임계값: {ROAD_THRESHOLD_M}m, 평행 컷오프: ≤60°")
    print(f"   대상 emd: {len(TARGET_PREFIX8)}개")

    # 스키마 확장
    print("\n[1] 스키마 확장...")
    for col, typ in [
        ("road_frontage_m", "REAL"),
        ("road_frontage_ratio", "REAL"),
        ("road_n_sides", "INTEGER"),
        ("is_corner_lot", "INTEGER"),
        ("road_frontage_angle_deg", "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE parcels ADD COLUMN {col} {typ}")
            print(f"   + {col}")
        except sqlite3.OperationalError:
            print(f"   - {col} (이미 있음)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_parcels_corner ON parcels(is_corner_lot)")
    conn.commit()

    print("\n[2] roads 격자 인덱스 구축...")
    t0 = time.time()
    road_idx, n_segs = load_road_index(conn)
    print(f"   {n_segs:,} 선분  /  {len(road_idx):,} 격자 셀  "
          f"({time.time()-t0:.1f}초)")

    print("\n[3] 대상 필지 로드...")
    ph = ",".join("?" * len(TARGET_PREFIX8))
    rows = list(conn.execute(
        f"SELECT pnu, geometry_json FROM parcels "
        f"WHERE prefix8 IN ({ph}) AND geometry_json IS NOT NULL",
        TARGET_PREFIX8))
    n_total = len(rows)
    print(f"   {n_total:,}필지")

    print("\n[4] frontage 계산...")
    t0 = time.time()
    updates = []
    n_with_frontage = 0
    n_corner = 0
    sides_dist = Counter()
    log_every = max(1, n_total // 10)
    for i, (pnu, geom) in enumerate(rows, 1):
        ff = frontage_features(geom, road_idx)
        if ff is None:
            updates.append((None, None, None, None, None, pnu))
            continue
        if ff["frontage_m"] > 0:
            n_with_frontage += 1
        if ff["is_corner"]:
            n_corner += 1
        sides_dist[ff["n_sides"]] += 1
        updates.append((
            ff["frontage_m"], ff["frontage_ratio"], ff["n_sides"],
            ff["is_corner"], ff["angle_deg"], pnu,
        ))
        if i % log_every == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n_total - i) / rate if rate > 0 else 0
            print(f"   {i:>6,}/{n_total:,}  접도 {n_with_frontage:>5,}건 "
                  f"({n_with_frontage/i*100:.1f}%)  "
                  f"속도 {rate:.0f}건/초  ETA {eta:.0f}초")
    print(f"   완료 ({time.time()-t0:.1f}초)")

    print("\n[5] DB 업데이트...")
    conn.executemany(
        "UPDATE parcels SET road_frontage_m=?, road_frontage_ratio=?, "
        "road_n_sides=?, is_corner_lot=?, road_frontage_angle_deg=? "
        "WHERE pnu=?",
        updates,
    )
    conn.commit()

    print("\n[6] 접도 변 수 분포")
    print("-" * 78)
    for k in sorted(sides_dist.keys()):
        n = sides_dist[k]
        label = "맹지" if k == 0 else (f"{k}면 접도")
        print(f"   {label:8s}: {n:>6,}건 ({n/n_total*100:>5.1f}%)")

    print("\n[7] 시군구별 접도 평균")
    print("-" * 78)
    sigg_name = {"41461": "처인구", "41463": "기흥구", "41465": "수지구"}
    for r in conn.execute(
        f"SELECT SUBSTR(prefix8,1,5) sgg, "
        f"  COUNT(*) total, "
        f"  SUM(CASE WHEN road_frontage_m>0 THEN 1 ELSE 0 END) with_front, "
        f"  ROUND(AVG(CASE WHEN road_frontage_m>0 THEN road_frontage_m END), 1) avg_len, "
        f"  ROUND(AVG(CASE WHEN road_frontage_m>0 THEN road_frontage_ratio END), 3) avg_ratio, "
        f"  SUM(is_corner_lot) corners "
        f"FROM parcels WHERE prefix8 IN ({ph}) AND road_n_sides IS NOT NULL "
        f"GROUP BY sgg ORDER BY 1",
        TARGET_PREFIX8
    ):
        name = sigg_name.get(r[0], r[0])
        pct = r[2] / r[1] * 100 if r[1] else 0
        corner_pct = r[5] / r[1] * 100 if r[1] else 0
        print(f"   {name} ({r[1]:>6,}필지): 접도 {pct:>5.1f}%  "
              f"평균길이 {r[3]}m  ratio {r[4]}  코너 {corner_pct:.1f}%")

    print("\n[8] 지목별 접도 (대지/임야/전/답)")
    print("-" * 78)
    for jimok in ("대", "임야", "전", "답", "공장용지"):
        r = conn.execute(
            f"SELECT COUNT(*), "
            f"  SUM(CASE WHEN road_frontage_m>0 THEN 1 ELSE 0 END), "
            f"  ROUND(AVG(CASE WHEN road_frontage_m>0 THEN road_frontage_m END), 1), "
            f"  SUM(is_corner_lot) "
            f"FROM parcels WHERE prefix8 IN ({ph}) AND jimok=? "
            f"AND road_n_sides IS NOT NULL",
            TARGET_PREFIX8 + (jimok,)
        ).fetchone()
        if not r or not r[0]:
            continue
        pct = r[1] / r[0] * 100
        corner_pct = (r[3] or 0) / r[0] * 100
        print(f"   {jimok:6s} ({r[0]:>5,}건): 접도 {pct:>5.1f}%  "
              f"평균 {r[2]}m  코너 {corner_pct:.1f}%")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
