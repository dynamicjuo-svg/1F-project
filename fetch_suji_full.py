"""
fetch_suji_full.py — 수지구 7 emd 각각 prefix8 단위 fetch (보강).
"""
import os, sqlite3, time, json as _json
from vworld_api import iter_pages
from build_db import polygon_total, JIMOK_MAP, extract_jimok_char

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    with open(os.path.join(HERE, "region_prefix_cache.json"), encoding="utf-8") as f:
        d = _json.load(f)
    targets = sorted(
        [(emd, info["prefix8"]) for emd, info in d["emd_map"].items()
         if info["prefix8"].startswith("41465")],
        key=lambda x: x[1],
    )
    print(f"수지구 {len(targets)} emd")

    total_new = 0
    for emd, p8 in targets:
        n_before = conn.execute(
            "SELECT COUNT(*) FROM parcels WHERE prefix8=?", (p8,)).fetchone()[0]
        print(f"  [{p8}] {emd:8s} fetch...", end=" ", flush=True)
        t0 = time.time()
        n_fetched = n_new = 0
        for ft, err in iter_pages(
            "LP_PA_CBND_BUBUN",
            attr_filter=f"pnu:like:{p8}",
            page_size=1000, geometry=True,
        ):
            if err:
                print(f"❌ {err}"); break
            p = ft.get("properties", {}); g = ft.get("geometry") or {}
            pnu = p.get("pnu")
            if not pnu: continue
            try:
                area_m2, cx, cy = polygon_total(g.get("coordinates"), g.get("type"))
            except Exception:
                area_m2, cx, cy = 0.0, 0.0, 0.0
            jibun = (p.get("jibun") or "").strip()
            bubun = (p.get("bubun") or "").strip()
            jimok_full = JIMOK_MAP.get(extract_jimok_char(jibun, bubun), "?")
            try: jiga = float(p.get("jiga") or 0)
            except: jiga = 0.0
            addr = p.get("addr") or ""
            try:
                geom_json = _json.dumps(g, ensure_ascii=False) if g.get("type") else None
            except Exception:
                geom_json = None
            n_fetched += 1
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO parcels "
                    "(pnu, jibun, jimok, area_m2, lon, lat, jiga, prefix8, addr, geometry_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pnu, jibun, jimok_full, area_m2, cx, cy, jiga, p8, addr, geom_json),
                )
                if conn.total_changes:
                    n_new += 1
            except Exception:
                pass
        conn.commit()
        n_after = conn.execute(
            "SELECT COUNT(*) FROM parcels WHERE prefix8=?", (p8,)).fetchone()[0]
        added = n_after - n_before
        total_new += added
        print(f"{n_fetched:>5,} fetched → {added:>5,} new (누적 {n_after:>5,}) [{time.time()-t0:.0f}s]")

    print(f"\n총 신규: {total_new:,} 필지")
    print("\n[최종] parcels 시군구별")
    for r in conn.execute(
        "SELECT SUBSTR(pnu,1,5), COUNT(*) FROM parcels GROUP BY SUBSTR(pnu,1,5) ORDER BY 1"):
        name = {"41461": "처인구", "41463": "기흥구", "41465": "수지구"}.get(r[0], r[0])
        print(f"  {r[0]} {name}: {r[1]:,}")


if __name__ == "__main__":
    main()
