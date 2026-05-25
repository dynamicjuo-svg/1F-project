"""
fetch_giheung_suji_parcels.py — 기흥구(41463)·수지구(41465) parcels 적재.

V-World LP_PA_CBND_BUBUN. Referer 헤더 적용.
prefix5(시군구)로 fetch → 응답 addr에서 prefix8/emd 자동 분류.
"""

import os
import sqlite3
import time
import json as _json

from vworld_api import iter_pages
from build_db import (polygon_total, JIMOK_MAP, extract_jimok_char,
                     init_db)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

TARGET_SIGGS = [
    ("41463", "용인 기흥구"),
    ("41465", "용인 수지구"),
]


def fetch_all_parcels(sigg5):
    """시군구 5자리 prefix로 모든 필지 fetch. (페이지네이션)"""
    rows = []
    addr_set = set()
    emd_by_prefix8 = {}  # prefix8 → set of emd names

    for ft, err in iter_pages(
        "LP_PA_CBND_BUBUN",
        attr_filter=f"pnu:like:{sigg5}",
        page_size=1000, geometry=True,
    ):
        if err:
            print(f"   ❌ V-World error: {err}")
            return rows, emd_by_prefix8
        p = ft.get("properties", {})
        g = ft.get("geometry") or {}
        pnu = p.get("pnu")
        if not pnu:
            continue
        prefix8 = pnu[:8]
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

        # geometry_json도 저장 (Polygon/MultiPolygon)
        try:
            geom_json = _json.dumps(g, ensure_ascii=False) if g.get("type") else None
        except Exception:
            geom_json = None

        rows.append((
            pnu, jibun, jimok_full, area_m2,
            cx, cy, jiga, prefix8, addr, geom_json,
        ))

        # addr에서 emd 추출 (prefix8별 emd 발견)
        if addr:
            parts = addr.split()
            for tok in parts:
                if tok.endswith(("읍", "면", "동")):
                    emd_by_prefix8.setdefault(prefix8, set()).add(tok)
                    break

        if len(rows) % 5000 == 0:
            print(f"   누적 {len(rows):,} 필지...", flush=True)

    return rows, emd_by_prefix8


def insert_parcels(conn, rows):
    """parcels INSERT OR IGNORE."""
    n_new = 0
    for row in rows:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO parcels "
                "(pnu, jibun, jimok, area_m2, lon, lat, jiga, prefix8, addr, geometry_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            if conn.total_changes:
                n_new += 1
        except Exception as e:
            pass
    return n_new


def update_region_cache(emd_by_prefix8, sigg5_to_name):
    """region_prefix_cache.json에 emd 추가."""
    cache_path = os.path.join(HERE, "region_prefix_cache.json")
    with open(cache_path, encoding="utf-8") as f:
        d = _json.load(f)
    if "emd_map" not in d:
        d["emd_map"] = {}

    for prefix8, emd_set in emd_by_prefix8.items():
        if not emd_set:
            continue
        # 가장 흔한 emd 선택 (단일이 정상)
        emd_name = sorted(emd_set)[0]
        if emd_name in d["emd_map"]:
            continue  # 이미 있으면 skip
        d["emd_map"][emd_name] = {
            "prefix8": prefix8,
            "sample_addr": "",
            "count": 0,
            "ri_list": [],
        }
    with open(cache_path, "w", encoding="utf-8") as f:
        _json.dump(d, f, ensure_ascii=False, indent=2)


def main():
    conn = init_db()
    # geometry_json 컬럼이 build_db.py 스키마에 있는지 확인
    cols = [r[1] for r in conn.execute("PRAGMA table_info(parcels)")]
    if "geometry_json" not in cols:
        try:
            conn.execute("ALTER TABLE parcels ADD COLUMN geometry_json TEXT")
            conn.commit()
        except Exception:
            pass

    print("=" * 78)
    print(" fetch_giheung_suji_parcels — 기흥·수지 parcels 적재")
    print("=" * 78)

    for sigg5, sigg_name in TARGET_SIGGS:
        print(f"\n[{sigg_name} {sigg5}] 적재 시작...")
        t0 = time.time()
        rows, emd_by_prefix8 = fetch_all_parcels(sigg5)
        print(f"   fetch 완료: {len(rows):,} 필지  ({time.time()-t0:.0f}초)")

        print(f"   prefix8별 발견된 emd:")
        for p8, emds in sorted(emd_by_prefix8.items()):
            print(f"     {p8}: {' · '.join(sorted(emds))}")

        print(f"   DB 적재 중...")
        n_new = insert_parcels(conn, rows)
        conn.commit()
        print(f"   {n_new:,} 건 신규 (중복 제외)")

        update_region_cache(emd_by_prefix8, {sigg5: sigg_name})

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
