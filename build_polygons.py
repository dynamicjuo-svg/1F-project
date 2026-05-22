"""
build_polygons.py — 백암면(41461350)+원삼면(41461340) 필지의 polygon을
V-World에서 받아 trades.db 의 parcels 테이블 geometry_json 컬럼에 저장.
"""

import json
import os
import sqlite3
import time

import requests
from api_keys import VWORLD_KEY


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

TEST_PREFIXES = [("원삼면", "41461340"), ("백암면", "41461350")]


def ensure_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(parcels)")]
    if "geometry_json" not in cols:
        print("• parcels.geometry_json 컬럼 추가")
        conn.execute("ALTER TABLE parcels ADD COLUMN geometry_json TEXT")
        conn.commit()
    else:
        print("• parcels.geometry_json 컬럼 이미 있음")


def fetch_polygons(prefix8):
    rows = []
    page = 1
    page_total = None
    while True:
        r = requests.get(
            "https://api.vworld.kr/req/data",
            params={
                "service": "data", "request": "GetFeature",
                "data": "LP_PA_CBND_BUBUN", "key": VWORLD_KEY,
                "attrFilter": f"pnu:like:{prefix8}",
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
            pnu = ft["properties"]["pnu"]
            g = ft.get("geometry") or {}
            if g:
                rows.append((json.dumps(g, ensure_ascii=False), pnu))
        print(f"   page {page}/{page_total}: 누적 {len(rows):,}")
        if page >= page_total:
            break
        page += 1
        time.sleep(0.1)
    return rows


def main():
    print("=" * 70)
    print(" 백암면 + 원삼면 필지 polygon 적재")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    ensure_column(conn)

    for name, prefix in TEST_PREFIXES:
        cur = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN geometry_json IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM parcels WHERE prefix8 = ?", (prefix,)
        )
        total, with_geom = cur.fetchone()
        with_geom = with_geom or 0
        if total > 0 and with_geom >= total * 0.95:
            print(f"\n✓ {name} ({prefix}): {with_geom:,}/{total:,} 이미 적재됨, 스킵")
            continue

        print(f"\n→ {name} ({prefix}) V-World에서 polygon 받는 중...")
        t0 = time.time()
        rows = fetch_polygons(prefix)
        elapsed = time.time() - t0
        print(f"   {len(rows):,}필지 다운로드 완료 ({elapsed:.1f}초)")

        conn.executemany(
            "UPDATE parcels SET geometry_json = ? WHERE pnu = ?", rows
        )
        conn.commit()
        print(f"   DB 업데이트 완료")

    # 최종 통계
    print("\n" + "=" * 70)
    print(" 최종 상태")
    print("=" * 70)
    for name, prefix in TEST_PREFIXES:
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN geometry_json IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM parcels WHERE prefix8 = ?", (prefix,)
        ).fetchone()
        print(f"   {name} ({prefix}): {row[1] or 0:,}/{row[0]:,}건에 polygon 적재")
    conn.close()


if __name__ == "__main__":
    main()
