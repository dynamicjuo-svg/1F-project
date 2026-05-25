"""
build_baekam_ri_mapping.py — 백암면 13개 prefix10 ↔ 리 매핑 추정 + parcels.addr 채움.

V-World 키가 불안해 사용 안 함. 대신 거래 데이터로 통계 추정:
- trades.umd_name="백암면 X리" 인 high 매칭 거래의 resolved_pnu prefix10
- 각 prefix10에서 가장 빈도 높은 리 = 그 prefix10의 정답 리

추정 후 parcels.addr 채움:
  "경기도 용인시 처인구 백암면 {ri} {jibun}"

→ rematch.py가 build_ri_prefix_map에서 자동으로 ri↔prefix10 매핑 학습.
"""

import sqlite3
import json
import os
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
REGION_CACHE = os.path.join(HERE, "region_prefix_cache.json")

BAEKAM_PREFIX8 = "41461350"


def estimate_ri_mapping(conn):
    """high 매칭 거래의 umd_name(리) ↔ resolved_pnu prefix10 빈도로 매핑 추정."""
    prefix_ri = defaultdict(Counter)
    for r in conn.execute(
        "SELECT umd_name, resolved_pnu FROM trades "
        "WHERE umd_name LIKE '%백암면%' AND match_confidence='high' "
        "AND resolved_pnu IS NOT NULL"
    ):
        p10 = r[0][:10] if r[0] else None
        if not r[1]:
            continue
        p10 = r[1][:10]
        parts = r[0].split() if r[0] else []
        if len(parts) >= 2 and parts[1].endswith("리"):
            prefix_ri[p10][parts[1]] += 1

    mapping = {}
    for p10, ctr in prefix_ri.items():
        if not p10.startswith(BAEKAM_PREFIX8):
            continue
        if not ctr:
            continue
        top_ri, top_n = ctr.most_common(1)[0]
        total = sum(ctr.values())
        confidence = top_n / total
        mapping[p10] = {"ri": top_ri, "confidence": confidence,
                         "support": top_n, "total": total}
    return mapping


def fill_parcels_addr(conn, mapping):
    """parcels.addr 채움 — 경기도 용인시 처인구 백암면 {ri} {jibun}."""
    n_updated = 0
    for p10, info in mapping.items():
        ri = info["ri"]
        for r in conn.execute(
            "SELECT pnu, jibun FROM parcels "
            "WHERE pnu LIKE ? AND (addr IS NULL OR addr='')",
            (p10 + "%",),
        ):
            pnu, jibun = r[0], r[1] or ""
            addr = f"경기도 용인시 처인구 백암면 {ri} {jibun.strip()}"
            conn.execute(
                "UPDATE parcels SET addr=? WHERE pnu=?",
                (addr, pnu),
            )
            n_updated += 1
    conn.commit()
    return n_updated


def update_region_cache(mapping):
    """region_prefix_cache.json에 백암면 prefix10→ri 매핑 명시 (rematch 활용)."""
    if not os.path.exists(REGION_CACHE):
        print(f"   {REGION_CACHE} 없음 — skip")
        return
    with open(REGION_CACHE, encoding="utf-8") as f:
        d = json.load(f)
    if "emd_map" not in d:
        d["emd_map"] = {}
    if "백암면" not in d["emd_map"]:
        d["emd_map"]["백암면"] = {
            "prefix8": BAEKAM_PREFIX8,
            "sample_addr": "경기도 용인시 처인구 백암면 근삼리 1445",
            "count": 0,
            "ri_list": [],
        }
    # prefix10 → ri 직접 매핑 추가
    d["emd_map"]["백암면"]["prefix10_ri"] = {
        p10: info["ri"] for p10, info in mapping.items()
    }
    d["emd_map"]["백암면"]["ri_list"] = sorted({
        info["ri"] for info in mapping.values()
    })
    with open(REGION_CACHE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" build_baekam_ri_mapping — 거래 데이터로 백암면 리 매핑 추정")
    print("=" * 78)

    print("\n[1] 거래 데이터에서 prefix10 ↔ ri 추정...")
    mapping = estimate_ri_mapping(conn)
    print(f"   {len(mapping)}개 prefix10 매핑됨")
    print()
    print("   prefix10    리        지지율(support/total)")
    print("   " + "-" * 50)
    for p10 in sorted(mapping.keys()):
        info = mapping[p10]
        flag = "★" if info["confidence"] >= 0.5 else (
            "·" if info["confidence"] >= 0.3 else "?")
        print(f"   {p10}  {info['ri']:6s}  "
              f"{info['support']:>3}/{info['total']:>3} "
              f"({info['confidence']*100:>5.1f}%) {flag}")

    print("\n[2] parcels.addr 채움 (백암면 33,892필지)...")
    n = fill_parcels_addr(conn, mapping)
    print(f"   {n:,}건 갱신")

    print("\n[3] region_prefix_cache.json 갱신...")
    update_region_cache(mapping)
    print("   완료")

    print("\n[4] 검증 — 백암면 ri별 parcels 개수")
    print("-" * 78)
    ri_counts = Counter()
    for r in conn.execute(
        f"SELECT addr FROM parcels WHERE prefix8='{BAEKAM_PREFIX8}'"
    ):
        if r[0]:
            parts = r[0].split()
            for tok in parts:
                if tok.endswith("리"):
                    ri_counts[tok] += 1
                    break
    for ri, n in ri_counts.most_common():
        print(f"   {ri}: {n:,}")

    conn.close()
    print("\n" + "=" * 78)
    print(" 다음: rematch.py 실행 → ri 매핑 자동 활용 + 매칭 정정")
    print("=" * 78)


if __name__ == "__main__":
    main()
