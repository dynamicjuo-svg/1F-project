"""
fetch_baekam_addr.py — 백암면 33,892 필지의 addr을 V-World에서 정확히 받아 parcels.addr 갱신.

이전: trades 통계로 prefix10→ri 추정 (지지율 22~75%)
지금: V-World 정답으로 교체

또 정확한 ri_prefix10 매핑 region_prefix_cache.json에 기록.
"""

import os
import sqlite3
import time
from collections import Counter

from vworld_api import iter_pages

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

BAEKAM_PREFIX8 = "41461350"


def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" fetch_baekam_addr — 백암면 33,892 필지 addr V-World로 정정")
    print("=" * 78)

    print("\n[1] V-World fetch (페이지 1000건씩)...")
    t0 = time.time()
    pnu_addr = {}  # PNU → addr
    page = 1
    for ft, err in iter_pages(
        "LP_PA_CBND_BUBUN",
        attr_filter=f"pnu:like:{BAEKAM_PREFIX8}",
        page_size=1000, geometry=False,
    ):
        if err:
            print(f"   ❌ V-World error: {err}")
            return
        p = ft.get("properties", {})
        pnu = p.get("pnu")
        addr = p.get("addr")
        if pnu and addr:
            pnu_addr[pnu] = addr
        # 진행 표시
        if len(pnu_addr) % 5000 == 0:
            print(f"   {len(pnu_addr):,} 건 누적  ({time.time()-t0:.0f}초)")
    print(f"   완료: {len(pnu_addr):,} 건  ({time.time()-t0:.0f}초)")

    print("\n[2] parcels.addr 갱신...")
    n_upd = 0
    for pnu, addr in pnu_addr.items():
        conn.execute("UPDATE parcels SET addr=? WHERE pnu=?", (addr, pnu))
        if conn.total_changes:
            n_upd += 1
    conn.commit()
    print(f"   {n_upd:,} 건 갱신")

    print("\n[3] prefix10 ↔ ri 정확 매핑 추출")
    prefix10_ri = {}
    for pnu, addr in pnu_addr.items():
        p10 = pnu[:10]
        # addr: "경기도 용인시 처인구 백암면 백봉리 산98"
        parts = addr.split()
        for tok in parts:
            if tok.endswith("리") and not tok[0].isdigit():
                prefix10_ri.setdefault(p10, Counter())[tok] += 1
                break

    print("\n   prefix10    리        지배율(top/total)  [확정]")
    print("   " + "-" * 60)
    final_map = {}
    for p10 in sorted(prefix10_ri.keys()):
        ctr = prefix10_ri[p10]
        top_ri, top_n = ctr.most_common(1)[0]
        total = sum(ctr.values())
        pct = top_n / total * 100
        flag = "★" if pct >= 95 else ("·" if pct >= 80 else "?")
        final_map[p10] = top_ri
        print(f"   {p10}  {top_ri:6s}  {top_n:>5}/{total:>5}  ({pct:>5.1f}%) {flag}")

    print("\n[4] region_prefix_cache.json 갱신")
    import json
    cache_path = os.path.join(HERE, "region_prefix_cache.json")
    with open(cache_path, encoding="utf-8") as f:
        d = json.load(f)
    if "백암면" not in d.get("emd_map", {}):
        d["emd_map"]["백암면"] = {
            "prefix8": BAEKAM_PREFIX8, "sample_addr": "",
            "count": 0, "ri_list": [],
        }
    d["emd_map"]["백암면"]["prefix10_ri"] = final_map
    d["emd_map"]["백암면"]["ri_list"] = sorted(set(final_map.values()))
    d["emd_map"]["백암면"]["count"] = len(pnu_addr)
    d["emd_map"]["백암면"]["sample_addr"] = next(iter(pnu_addr.values()), "")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print("   완료")

    print("\n[5] 검증 — V-World 정답 vs 이전 통계 추정 비교")
    print("-" * 78)
    prev_estimate = {
        '4146135021': '백암리', '4146135022': '박곡리', '4146135023': '백봉리',
        '4146135024': '고안리', '4146135025': '옥산리', '4146135026': '장평리',
        '4146135027': '석천리', '4146135028': '근창리', '4146135029': '근삼리',
        '4146135030': '근창리', '4146135031': '근곡리', '4146135032': '가창리',
        '4146135033': '가좌리',
    }
    n_match = n_diff = 0
    for p10, true_ri in final_map.items():
        est = prev_estimate.get(p10, '?')
        if est == true_ri:
            n_match += 1
            mark = '✓'
        else:
            n_diff += 1
            mark = '✗'
        print(f"   {p10} {mark}  추정={est:6s}  정답={true_ri:6s}")
    print(f"\n   추정 일치: {n_match}/{len(final_map)}")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
