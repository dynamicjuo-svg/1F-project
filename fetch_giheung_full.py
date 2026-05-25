"""
fetch_giheung_full.py — 기흥구 18개 emd 각각 prefix8 단위로 V-World 호출.

prefix5(시군구) 단위는 V-World 페이지 한도에 걸려 일부만 받음.
prefix8(emd) 단위면 각 emd 모든 필지 받음.
"""

import os
import sqlite3
import time
import json as _json

from vworld_api import iter_pages
from build_db import polygon_total, JIMOK_MAP, extract_jimok_char

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

# 기흥구 18개 emd (region_prefix_cache.json에서 가져옴)
def get_target_prefix8s():
    cache_path = os.path.join(HERE, "region_prefix_cache.json")
    with open(cache_path, encoding="utf-8") as f:
        d = _json.load(f)
    targets = []
    for emd, info in d.get("emd_map", {}).items():
        p8 = info.get("prefix8", "")
        if p8.startswith("41463"):
            targets.append((emd, p8))
    return sorted(targets, key=lambda x: x[1])


def fetch_prefix8(prefix8):
    """단일 prefix8의 모든 필지 fetch."""
    rows = []
    for ft, err in iter_pages(
        "LP_PA_CBND_BUBUN",
        attr_filter=f"pnu:like:{prefix8}",
        page_size=1000, geometry=True,
    ):
        if err:
            print(f"     ❌ {err}")
            return rows
        p = ft.get("properties", {})
        g = ft.get("geometry") or {}
        pnu = p.get("pnu")
        if not pnu:
            continue
        try:
            area_m2, cx, cy = polygon_total(g.get("coordinates"), g.get("type"))
        except Exception:
            area_m2, cx, cy = 0.0, 0.0, 0.0
        jibun = (p.get("jibun") or "").strip()
        bubun = (p.get("bubun") or "").strip()
        jimok_full = JIMOK_MAP.get(extract_jimok_char(jibun, bubun), "?")
        try:
            jiga = float(p.get("jiga") or 0)
        except (TypeError, ValueError):
            jiga = 0.0
        addr = p.get("addr") or ""
        try:
            geom_json = _json.dumps(g, ensure_ascii=False) if g.get("type") else None
        except Exception:
            geom_json = None
        rows.append((
            pnu, jibun, jimok_full, area_m2,
            cx, cy, jiga, prefix8, addr, geom_json,
        ))
    return rows


def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" fetch_giheung_full — 기흥구 18개 emd 각각 fetch")
    print("=" * 78)

    targets = get_target_prefix8s()
    print(f"\n대상 emd: {len(targets)}개")
    print()

    total_new = 0
    for emd, p8 in targets:
        # 이미 적재 수 확인
        n_before = conn.execute(
            "SELECT COUNT(*) FROM parcels WHERE prefix8=?", (p8,)
        ).fetchone()[0]
        print(f"  [{p8}] {emd:8s}  fetch...", end=" ", flush=True)
        t0 = time.time()
        rows = fetch_prefix8(p8)
        n_fetched = len(rows)
        n_inserted = 0
        for row in rows:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO parcels "
                    "(pnu, jibun, jimok, area_m2, lon, lat, jiga, prefix8, addr, geometry_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                if conn.total_changes:
                    n_inserted += 1
            except Exception:
                pass
        conn.commit()
        n_after = conn.execute(
            "SELECT COUNT(*) FROM parcels WHERE prefix8=?", (p8,)
        ).fetchone()[0]
        total_new += (n_after - n_before)
        print(f"{n_fetched:>5,} fetched  →  {n_after - n_before:>5,} new  "
              f"(누적 {n_after:>5,})  [{time.time()-t0:.0f}s]")

    print(f"\n총 신규: {total_new:,} 필지")

    # 최종 통계
    print("\n[최종] parcels 시군구별")
    print("-" * 78)
    for r in conn.execute(
        "SELECT SUBSTR(pnu,1,5), COUNT(*) FROM parcels GROUP BY SUBSTR(pnu,1,5) ORDER BY 1"
    ):
        sigg_name = {"41461": "용인 처인구", "41463": "용인 기흥구",
                    "41465": "용인 수지구"}.get(r[0], r[0])
        print(f"  {r[0]} {sigg_name:12s}: {r[1]:>7,}필지")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
