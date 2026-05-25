"""
fetch_roads_giheung_suji.py — 기흥+수지 박스 V-World LT_L_MOCTLINK 보강 적재.

기존 roads (처인구 박스 ±5km)에 link_id UPSERT.
박스 겹침 영역의 도로는 link_id 동일 → INSERT OR REPLACE로 자연스럽게 dedup.
"""

import json
import os
import sqlite3
import time

from vworld_api import iter_pages

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")


def main():
    conn = sqlite3.connect(DB_PATH)

    # 기흥+수지 박스 ±5km
    b = conn.execute(
        "SELECT MIN(lon), MAX(lon), MIN(lat), MAX(lat) FROM parcels "
        "WHERE prefix8 LIKE '41463%' OR prefix8 LIKE '41465%'"
    ).fetchone()
    margin = 0.05
    bbox = f"{b[0]-margin:.4f},{b[2]-margin:.4f},{b[1]+margin:.4f},{b[3]+margin:.4f}"
    print("=" * 78)
    print(" fetch_roads_giheung_suji — V-World LT_L_MOCTLINK 보강 (기흥+수지 박스)")
    print("=" * 78)
    print(f"   bbox: {bbox}")

    n_before = conn.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
    print(f"   roads 기존: {n_before:,}건")

    print("\n[1] V-World fetch...")
    t0 = time.time()
    n_fetched = 0
    n_inserted = 0
    n_updated = 0
    n_new_link = 0
    rows = []
    for ft, err in iter_pages(
        "LT_L_MOCTLINK",
        geom_filter=f"BOX({bbox})",
        page_size=1000, geometry=True,
    ):
        if err:
            print(f"   ❌ {err}")
            break
        p = ft.get("properties", {})
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
        n_fetched += 1
        if n_fetched % 5000 == 0:
            print(f"   누적 {n_fetched:,}")
    print(f"   {n_fetched:,} fetched  ({time.time()-t0:.1f}초)")

    print("\n[2] DB UPSERT...")
    for row in rows:
        link_id = row[0]
        exists = conn.execute(
            "SELECT 1 FROM roads WHERE link_id=?", (link_id,)).fetchone()
        if exists:
            n_updated += 1
        else:
            n_new_link += 1
        conn.execute(
            "INSERT OR REPLACE INTO roads "
            "(link_id, road_name, rd_rank_h, rd_type_h, max_spd, "
            "geometry_json, min_lon, max_lon, min_lat, max_lat) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    conn.commit()

    n_after = conn.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
    print(f"   신규 link {n_new_link:,}건  /  기존 갱신 {n_updated:,}건")
    print(f"   roads 총합: {n_before:,} → {n_after:,}  (+{n_after-n_before:,})")

    print("\n[3] 도로 위계별 (전체)")
    for r in conn.execute(
        "SELECT rd_rank_h, COUNT(*) FROM roads GROUP BY rd_rank_h "
        "ORDER BY 2 DESC"
    ):
        print(f"   {r[1]:>6,}  {r[0]}")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
