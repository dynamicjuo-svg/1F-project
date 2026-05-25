"""
build_parcel_shape.py — 필지 형상(Shape) 자체 계산.

산출 컬럼 (모두 0~1 정규화):
  - shape_aspect:     MBR 짧은변/긴변. 1=정사각형, 0=무한 길쭉
  - shape_compactness: 4π·area / perimeter². 0.785=정사각형, <0.5=길쭉/복잡
  - shape_rect_fill:  polygon_area / mbr_area. 1=완전 직사각형
  - shape_convexity:  polygon_area / convex_hull_area. 1=볼록, <1=오목(L자)
  - shape_type:       5-bin 분류 (정방형/장방형/길쭉형/L자형/불규칙)

외부 의존: numpy(이미 pandas 포함). shapely·rasterio 등 미사용.
대상: 원삼면+백암면 68,393필지 (geometry 100% 적재).
"""

import json
import math
import os
import sqlite3
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
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


# =====================================================================
#  Helpers — convex hull, MBR, geometry features (자체 구현)
# =====================================================================
def _to_xy_m(ring, lat0):
    """경위도(deg) → 미터 평면(lat=lat0 기준). MBR/거리 계산용."""
    lon_m = LAT_DEG_TO_M * math.cos(math.radians(lat0))
    lat_m = LAT_DEG_TO_M
    return [(p[0] * lon_m, p[1] * lat_m) for p in ring]


def _polygon_area_perimeter(pts):
    """Shoelace + 변 길이 합."""
    n = len(pts)
    if n < 3:
        return 0.0, 0.0
    s = 0.0
    perim = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
        perim += math.hypot(x2 - x1, y2 - y1)
    return abs(s) / 2.0, perim


def _convex_hull(pts):
    """Andrew's monotone chain. O(n log n). 반환: hull 정점 리스트(CCW)."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _min_bounding_rectangle(hull):
    """Rotating Calipers — convex hull에 대한 최소 외접 직사각형.
    반환: (mbr_area, mbr_long, mbr_short).
    O(n²) but hull 정점 적어 충분히 빠름.
    """
    if len(hull) < 2:
        return 0.0, 0.0, 0.0
    best_area = float("inf")
    best_long = best_short = 0.0
    n = len(hull)
    for i in range(n):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % n]
        edge_len = math.hypot(x2 - x1, y2 - y1)
        if edge_len == 0:
            continue
        # 단위 벡터 (edge 방향)
        ex, ey = (x2 - x1) / edge_len, (y2 - y1) / edge_len
        # 수직 단위 벡터
        nx, ny = -ey, ex
        # hull 정점들을 (edge방향, 수직) 좌표계에 투영
        min_u = max_u = (hull[0][0] - x1) * ex + (hull[0][1] - y1) * ey
        min_v = max_v = (hull[0][0] - x1) * nx + (hull[0][1] - y1) * ny
        for p in hull[1:]:
            u = (p[0] - x1) * ex + (p[1] - y1) * ey
            v = (p[0] - x1) * nx + (p[1] - y1) * ny
            if u < min_u: min_u = u
            if u > max_u: max_u = u
            if v < min_v: min_v = v
            if v > max_v: max_v = v
        w = max_u - min_u
        h = max_v - min_v
        area = w * h
        if area < best_area:
            best_area = area
            best_long, best_short = max(w, h), min(w, h)
    return best_area, best_long, best_short


def _outer_ring(geom):
    """geometry_json → outer ring 좌표 리스트 (가장 큰 폴리곤 기준)."""
    if not isinstance(geom, dict):
        return None
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t == "Polygon":
        return coords[0] if coords else None
    elif t == "MultiPolygon":
        # 가장 큰 폴리곤 외곽환 (대표 형상)
        if not coords:
            return None
        biggest = None
        biggest_area = -1
        for poly in coords:
            if not poly:
                continue
            ring = poly[0]
            # 간이 면적 (degree 단위)으로 비교 — 정확치 아니어도 순위는 OK
            n = len(ring)
            s = 0.0
            for i in range(n):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                s += x1 * y2 - x2 * y1
            a = abs(s) / 2.0
            if a > biggest_area:
                biggest_area = a
                biggest = ring
        return biggest
    return None


# =====================================================================
#  Features
# =====================================================================
def shape_features(geometry_json):
    """outer ring → (aspect, compactness, rect_fill, convexity).
    geometry/계산 실패 시 None."""
    try:
        g = json.loads(geometry_json) if isinstance(geometry_json, str) else geometry_json
    except (TypeError, json.JSONDecodeError):
        return None
    ring = _outer_ring(g)
    if not ring or len(ring) < 3:
        return None
    # 평균 위도 기준 평면 변환
    lat0 = sum(p[1] for p in ring) / len(ring)
    pts = _to_xy_m(ring, lat0)
    poly_area, perim = _polygon_area_perimeter(pts)
    if poly_area <= 0 or perim <= 0:
        return None
    # Convex hull
    hull = _convex_hull(pts)
    if len(hull) < 3:
        return None
    hull_area, _ = _polygon_area_perimeter(hull)
    if hull_area <= 0:
        return None
    # MBR
    mbr_area, mbr_long, mbr_short = _min_bounding_rectangle(hull)
    if mbr_area <= 0 or mbr_long <= 0:
        return None
    # Features
    aspect = mbr_short / mbr_long          # 0~1
    compactness = 4 * math.pi * poly_area / (perim * perim)  # 0~1 (원=1)
    rect_fill = poly_area / mbr_area       # 0~1
    convexity = poly_area / hull_area       # 0~1
    return aspect, compactness, rect_fill, convexity


def classify_shape(aspect, compactness, rect_fill, convexity):
    """5-bin 분류 — 정방형/장방형/길쭉형/L자형/불규칙."""
    if convexity < 0.85:
        return "L자형"
    if rect_fill < 0.65:
        return "불규칙"
    if aspect >= 0.75 and rect_fill >= 0.85:
        return "정방형"
    if aspect >= 0.4 and rect_fill >= 0.75:
        return "장방형"
    if aspect < 0.4:
        return "길쭉형"
    return "불규칙"


# =====================================================================
#  Main
# =====================================================================
def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" build_parcel_shape — 필지 형상 자체 계산 + 5-bin 분류")
    print("=" * 78)

    # 컬럼 추가 (없으면)
    print("\n[1] 스키마 확장...")
    for col, typ in [
        ("shape_aspect", "REAL"), ("shape_compactness", "REAL"),
        ("shape_rect_fill", "REAL"), ("shape_convexity", "REAL"),
        ("shape_type", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE parcels ADD COLUMN {col} {typ}")
            print(f"   + {col}")
        except sqlite3.OperationalError:
            print(f"   - {col} (이미 있음)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_parcels_shape ON parcels(shape_type)")
    conn.commit()

    print("\n[2] 대상 필지 로드...")
    ph = ",".join("?" * len(TARGET_PREFIX8))
    rows = list(conn.execute(
        f"SELECT pnu, geometry_json FROM parcels "
        f"WHERE prefix8 IN ({ph}) AND geometry_json IS NOT NULL",
        TARGET_PREFIX8))
    print(f"   {len(rows):,}필지")

    print("\n[3] 형상 feature 계산...")
    t0 = time.time()
    updates = []
    n_total = len(rows)
    n_fail = 0
    n_shape = Counter()
    log_every = max(1, n_total // 10)
    for i, (pnu, geom_str) in enumerate(rows, 1):
        feat = shape_features(geom_str)
        if feat is None:
            n_fail += 1
            updates.append((None, None, None, None, None, pnu))
            continue
        aspect, compact, rect_fill, convex = feat
        stype = classify_shape(aspect, compact, rect_fill, convex)
        n_shape[stype] += 1
        updates.append((aspect, compact, rect_fill, convex, stype, pnu))
        if i % log_every == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(f"   {i:>6,}/{n_total:,}  ({i/n_total*100:>5.1f}%)  "
                  f"속도 {rate:.0f}건/초  실패 {n_fail}")
    print(f"   완료 ({time.time()-t0:.1f}초)  실패 {n_fail}")

    print("\n[4] DB 업데이트...")
    conn.executemany(
        "UPDATE parcels SET shape_aspect=?, shape_compactness=?, "
        "shape_rect_fill=?, shape_convexity=?, shape_type=? WHERE pnu=?",
        updates,
    )
    conn.commit()

    print("\n[5] Shape 분포")
    print("-" * 78)
    total = sum(n_shape.values())
    for k in ["정방형", "장방형", "길쭉형", "L자형", "불규칙"]:
        n = n_shape.get(k, 0)
        pct = n / total * 100 if total else 0
        print(f"   {k:8s}: {n:>6,}건 ({pct:>5.1f}%)")
    print(f"   {'합계':8s}: {total:>6,}건")

    print("\n[6] 지목별 형상 분포 (상위 5 지목)")
    print("-" * 78)
    for r in conn.execute(
        f"SELECT jimok, COUNT(*) cnt FROM parcels "
        f"WHERE prefix8 IN ({ph}) GROUP BY jimok ORDER BY 2 DESC LIMIT 5",
        TARGET_PREFIX8
    ):
        jimok = r[0]
        breakdown = {}
        for r2 in conn.execute(
            f"SELECT shape_type, COUNT(*) FROM parcels "
            f"WHERE prefix8 IN ({ph}) AND jimok=? AND shape_type IS NOT NULL "
            f"GROUP BY shape_type",
            TARGET_PREFIX8 + (jimok,)
        ):
            breakdown[r2[0]] = r2[1]
        line = f"   {jimok:6s} ({r[1]:>5,}건): "
        line += "  ".join(
            f"{k} {breakdown.get(k,0):>4,}({breakdown.get(k,0)/max(1,r[1])*100:>4.1f}%)"
            for k in ["정방형", "장방형", "길쭉형", "L자형", "불규칙"]
        )
        print(line)

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
