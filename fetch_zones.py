"""
fetch_zones.py — V-World LT_C_UQ111 (용도지역) 적재 + 필지별 zone 부여.

V-World LT_C_UQ111 attribute (probe로 확인):
  - uname: 용도지역 이름 (예: "준주거지역", "일반공업지역", "보전관리지역")
  - dyear, dnum: 결정연도/번호
  - sigg_name: 시군구

매칭 알고리즘:
  1. 용인시 전체 zone polygon fetch (geometry=true)
  2. 필지 centroid (lon, lat) → point-in-polygon 으로 어느 zone 인지
  3. parcels.zone_type (대분류), zone_detail (세분) UPDATE
"""

import json
import os
import sqlite3
import time
from collections import Counter

from vworld_api import iter_pages

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")


# 대분류 매핑 (uname → zone_type)
def classify_zone_type(uname):
    if not uname:
        return None
    s = uname
    if any(k in s for k in ["주거", "상업", "공업", "녹지"]):
        return "도시지역"
    if "관리지역" in s:
        return "관리지역"
    if "농림" in s:
        return "농림지역"
    if "자연환경보전" in s:
        return "자연환경보전지역"
    return "기타"


def init_zones_table(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS zones (
        zone_id TEXT PRIMARY KEY,
        uname TEXT,
        zone_type TEXT,
        dyear TEXT,
        dnum TEXT,
        sigg_name TEXT,
        geometry_json TEXT,
        min_lon REAL, max_lon REAL,
        min_lat REAL, max_lat REAL
    );
    CREATE INDEX IF NOT EXISTS idx_zones_uname ON zones(uname);
    CREATE INDEX IF NOT EXISTS idx_zones_bbox ON zones(min_lon, max_lon, min_lat, max_lat);
    """)
    conn.commit()


def _ring_bbox(ring):
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return min(lons), max(lons), min(lats), max(lats)


def point_in_ring(px, py, ring):
    """Ray casting. ring=[(lon,lat),...]"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def point_in_zone(px, py, geom):
    """zone geometry_json → True if (px,py) inside outer rings (any polygon)."""
    if not geom:
        return False
    t = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return False
    if t == "Polygon":
        polys = [coords]
    elif t == "MultiPolygon":
        polys = coords
    else:
        return False
    for poly in polys:
        if not poly:
            continue
        outer = poly[0]
        if not point_in_ring(px, py, outer):
            continue
        # 내부 hole 검사
        in_hole = False
        for hole in poly[1:]:
            if point_in_ring(px, py, hole):
                in_hole = True
                break
        if not in_hole:
            return True
    return False


def main():
    conn = sqlite3.connect(DB_PATH)
    init_zones_table(conn)

    print("=" * 78)
    print(" fetch_zones — V-World LT_C_UQ111 용도지역 (용인시 전체)")
    print("=" * 78)

    n_zones_before = conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
    print(f"   zones 기존: {n_zones_before:,}건")

    # parcels 컬럼 (zone_type, zone_detail은 build_db에서 이미 추가됨)
    # 확인용:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(parcels)")]
    for c in ("zone_type", "zone_detail"):
        if c not in cols:
            conn.execute(f"ALTER TABLE parcels ADD COLUMN {c} TEXT")
            print(f"   + parcels.{c}")
    conn.commit()

    # 용인시 전체 박스 ±5km (한글 attr_filter 인코딩 우회)
    b = conn.execute(
        "SELECT MIN(lon), MAX(lon), MIN(lat), MAX(lat) FROM parcels "
        "WHERE prefix8 LIKE '41461%' OR prefix8 LIKE '41463%' OR prefix8 LIKE '41465%'"
    ).fetchone()
    margin = 0.05
    bbox = f"{b[0]-margin:.4f},{b[2]-margin:.4f},{b[1]+margin:.4f},{b[3]+margin:.4f}"
    print(f"\n[1] V-World LT_C_UQ111 fetch (BOX {bbox})...")
    t0 = time.time()
    n_fetched = 0
    rows = []
    seen_ids = set()
    for ft, err in iter_pages(
        "LT_C_UQ111",
        geom_filter=f"BOX({bbox})",
        page_size=1000, geometry=True,
    ):
        if err:
            print(f"   ❌ {err}")
            break
        p = ft.get("properties", {})
        g = ft.get("geometry") or {}
        zid = ft.get("id") or ""
        if not zid or zid in seen_ids:
            continue
        seen_ids.add(zid)
        uname = p.get("uname") or ""
        if not g or not g.get("type"):
            continue
        # bbox 계산
        coords = g.get("coordinates") or []
        all_lons, all_lats = [], []
        def _walk(c):
            if not c: return
            if isinstance(c[0], (int, float)):
                all_lons.append(c[0]); all_lats.append(c[1])
            else:
                for x in c: _walk(x)
        _walk(coords)
        if not all_lons:
            continue
        rows.append((
            zid, uname, classify_zone_type(uname),
            p.get("dyear") or "", p.get("dnum") or "",
            p.get("sigg_name") or "", json.dumps(g, ensure_ascii=False),
            min(all_lons), max(all_lons), min(all_lats), max(all_lats),
        ))
        n_fetched += 1
        if n_fetched % 500 == 0:
            print(f"   누적 {n_fetched:,}")
    print(f"   {n_fetched:,} fetched ({time.time()-t0:.1f}초)")

    print("\n[2] zones DB UPSERT...")
    conn.executemany(
        "INSERT OR REPLACE INTO zones "
        "(zone_id, uname, zone_type, dyear, dnum, sigg_name, geometry_json, "
        "min_lon, max_lon, min_lat, max_lat) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    n_zones = conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
    print(f"   zones 총합: {n_zones:,}")

    # 용도지역 분포
    print("\n[3] 용도지역 종류별 (zones 폴리곤 기준)")
    print("-" * 78)
    for r in conn.execute(
        "SELECT uname, zone_type, COUNT(*) FROM zones "
        "GROUP BY uname, zone_type ORDER BY 3 DESC LIMIT 30"):
        zt = (r[1] or "?")[:8]
        print(f"   {r[2]:>5,}  [{zt:8s}] {r[0]}")

    print("\n[4] 필지별 zone 매칭 시작...")
    # 모든 zones 메모리에 로드
    zone_list = []
    for r in conn.execute(
        "SELECT zone_id, uname, zone_type, geometry_json, "
        "min_lon, max_lon, min_lat, max_lat FROM zones"):
        try:
            g = json.loads(r[3])
        except Exception:
            continue
        zone_list.append({
            "id": r[0], "uname": r[1], "type": r[2], "geom": g,
            "bbox": (r[4], r[5], r[6], r[7]),
        })
    print(f"   {len(zone_list):,} zones 로드")

    # 격자 인덱스 (bbox 기반)
    GRID = 0.01
    grid_idx = {}
    for i, z in enumerate(zone_list):
        ml, Ml, mt, Mt = z["bbox"]
        gx0, gx1 = int(ml / GRID), int(Ml / GRID)
        gy0, gy1 = int(mt / GRID), int(Mt / GRID)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                grid_idx.setdefault((gx, gy), []).append(i)

    # parcels 매칭
    print("   parcels 로드...")
    rows = list(conn.execute(
        "SELECT pnu, lon, lat FROM parcels "
        "WHERE lon IS NOT NULL AND lat IS NOT NULL "
        "AND (prefix8 LIKE '41461%' OR prefix8 LIKE '41463%' OR prefix8 LIKE '41465%')"))
    n_total = len(rows)
    print(f"   {n_total:,} 필지")

    t0 = time.time()
    updates = []
    n_match = 0
    n_miss = 0
    type_dist = Counter()
    log_every = max(1, n_total // 10)
    for i, (pnu, lon, lat) in enumerate(rows, 1):
        gx, gy = int(lon / GRID), int(lat / GRID)
        match_type = match_detail = None
        for zi in grid_idx.get((gx, gy), ()):
            z = zone_list[zi]
            ml, Ml, mt, Mt = z["bbox"]
            if not (ml <= lon <= Ml and mt <= lat <= Mt):
                continue
            if point_in_zone(lon, lat, z["geom"]):
                match_type = z["type"]
                match_detail = z["uname"]
                break
        if match_type:
            n_match += 1
            type_dist[match_type] += 1
            updates.append((match_type, match_detail, pnu))
        else:
            n_miss += 1
            updates.append((None, None, pnu))
        if i % log_every == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n_total - i) / rate if rate > 0 else 0
            print(f"   {i:>7,}/{n_total:,}  매칭 {n_match:>6,} ({n_match/i*100:.1f}%)  "
                  f"속도 {rate:.0f}/s  ETA {eta:.0f}초")
    print(f"   완료 ({time.time()-t0:.1f}초)")

    print("\n[5] DB 업데이트 (parcels.zone_*)...")
    conn.executemany(
        "UPDATE parcels SET zone_type=?, zone_detail=? WHERE pnu=?",
        updates,
    )
    conn.commit()

    print("\n[6] 매칭 결과")
    print("-" * 78)
    print(f"   매칭: {n_match:,} / {n_total:,} ({n_match/n_total*100:.1f}%)")
    for k, v in type_dist.most_common():
        print(f"   {k:14s}: {v:>7,}")

    print("\n[7] 시군구별 zone_type")
    print("-" * 78)
    sigg_name = {"41461": "처인구", "41463": "기흥구", "41465": "수지구"}
    for sgg in ("41461", "41463", "41465"):
        breakdown = {}
        for r in conn.execute(
            "SELECT zone_type, COUNT(*) FROM parcels "
            f"WHERE prefix8 LIKE '{sgg}%' AND zone_type IS NOT NULL "
            "GROUP BY zone_type ORDER BY 2 DESC"):
            breakdown[r[0]] = r[1]
        tot = sum(breakdown.values())
        name = sigg_name[sgg]
        line = f"   {name} ({tot:>6,}): "
        line += "  ".join(
            f"{k} {v:>5,}({v/tot*100:>4.1f}%)" for k, v in breakdown.items())
        print(line)

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
