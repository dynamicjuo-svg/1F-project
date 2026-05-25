"""
build_price_anomaly.py — 평단가 이상치 자동 라벨링.

알고리즘:
  1. match_confidence='high' AND share_label IS NULL (정상 매칭 단독)
  2. group by (umd_name, jimok)
  3. 그룹 표본 ≥ 5만 분석 (작은 그룹은 통계 의미 없음)
  4. IQR(사분위) 기반 이상치 — Q3 + 1.5*IQR 초과 'high_outlier',
     Q1 - 1.5*IQR 미만 'low_outlier'
  5. trades.price_anomaly 컬럼에 저장

이상치는 권리분석에서 의심해볼 거래:
  - high_outlier: 시세 대비 비정상적 고가 (특수거래·공유지분·인근 거래 패턴 검토)
  - low_outlier:  시세 대비 비정상적 저가 (지분·가족간·급매·매칭 의심)
"""

import os
import sqlite3
import statistics
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

MIN_GROUP_SIZE = 5    # 그룹 표본 최소
IQR_MULTIPLIER = 1.5  # 표준 outlier 룰


def quartiles(sorted_vals):
    """Q1, Q3 계산 (linear interpolation)."""
    n = len(sorted_vals)
    if n < 4:
        return sorted_vals[0], sorted_vals[-1]
    q1_pos = (n - 1) * 0.25
    q3_pos = (n - 1) * 0.75
    def at(pos):
        lo = int(pos)
        frac = pos - lo
        if lo + 1 >= n:
            return sorted_vals[lo]
        return sorted_vals[lo] + (sorted_vals[lo + 1] - sorted_vals[lo]) * frac
    return at(q1_pos), at(q3_pos)


def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" build_price_anomaly — 평단가 이상치 자동 라벨")
    print("=" * 78)

    print("\n[1] 스키마 확장...")
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN price_anomaly TEXT")
        print("   + price_anomaly TEXT 추가")
    except sqlite3.OperationalError:
        print("   - price_anomaly (이미 있음)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_price_anomaly "
        "ON trades(price_anomaly)"
    )
    conn.execute("UPDATE trades SET price_anomaly = NULL")
    conn.commit()

    print("\n[2] 그룹별 평단가 분포 분석...")
    rows = list(conn.execute(
        "SELECT id, umd_name, jimok, unit_per_pyeong "
        "FROM trades "
        "WHERE match_confidence='high' AND share_label IS NULL "
        "AND unit_per_pyeong IS NOT NULL AND unit_per_pyeong > 0"
    ))
    print(f"   분석 대상 (high·share_label NULL): {len(rows):,}건")

    # 그룹화 — (emd 첫 토큰, jimok)
    groups = defaultdict(list)
    id_to_group = {}
    for r in rows:
        emd_first = (r[1] or "").split()[0] if r[1] else ""
        key = (emd_first, r[2])  # (emd, jimok)
        groups[key].append((r[0], r[3]))
        id_to_group[r[0]] = key

    print(f"   그룹 수: {len(groups):,}  (표본 ≥{MIN_GROUP_SIZE} 그룹만 분석)")

    print("\n[3] IQR 기반 이상치 탐지...")
    updates = []
    n_high = 0
    n_low = 0
    skipped_small = 0
    sample_groups = []   # 디버그용
    for key, items in groups.items():
        if len(items) < MIN_GROUP_SIZE:
            skipped_small += 1
            continue
        vals = sorted(x[1] for x in items)
        q1, q3 = quartiles(vals)
        iqr = q3 - q1
        upper = q3 + IQR_MULTIPLIER * iqr
        lower = q1 - IQR_MULTIPLIER * iqr
        # 라벨링
        for tid, unit in items:
            if unit > upper:
                updates.append(("high_outlier", tid))
                n_high += 1
            elif unit < lower:
                updates.append(("low_outlier", tid))
                n_low += 1
        if len(sample_groups) < 8:
            sample_groups.append((key, len(items), q1, q3, upper, lower))

    print(f"   분석된 그룹: {len(groups) - skipped_small:,}  "
          f"표본 작아 skip: {skipped_small:,}")
    print(f"   이상치: high_outlier {n_high:,}  low_outlier {n_low:,}")

    print("\n[4] DB 업데이트...")
    conn.executemany(
        "UPDATE trades SET price_anomaly=? WHERE id=?",
        updates,
    )
    conn.commit()
    print(f"   {len(updates):,}건 라벨링")

    print("\n[5] 샘플 그룹 임계값")
    print("-" * 78)
    print(f"   {'emd':10s} {'jimok':6s}  n   Q1     Q3     상한   하한")
    for key, n, q1, q3, upper, lower in sample_groups:
        emd, jimok = key
        print(f"   {emd:10s} {jimok:6s}  {n:>3} "
              f"{q1:>5,.0f}  {q3:>5,.0f}  {upper:>5,.0f}  {lower:>5,.0f}")

    print("\n[6] 이상치 케이스 샘플")
    print("-" * 78)
    print("[고평가 의심 상위 5건]")
    for r in conn.execute(
        "SELECT umd_name, jimok, jibun_masked, area_m2, deal_amount, "
        "unit_per_pyeong, deal_ymd FROM trades "
        "WHERE price_anomaly='high_outlier' "
        "ORDER BY unit_per_pyeong DESC LIMIT 5"
    ):
        py = r[3] / 3.3058 if r[3] else 0
        print(f"   {r[6][:10]}  {r[0]:14s} {r[1]:4s}  "
              f"mask={r[2]:8s}  {r[3]:>6,.0f}㎡({py:>4,.0f}평)  "
              f"{r[4]:>6,}만원  {r[5]:>5,.0f}만/평")
    print()
    print("[저평가 의심 상위 5건]")
    for r in conn.execute(
        "SELECT umd_name, jimok, jibun_masked, area_m2, deal_amount, "
        "unit_per_pyeong, deal_ymd FROM trades "
        "WHERE price_anomaly='low_outlier' "
        "ORDER BY unit_per_pyeong ASC LIMIT 5"
    ):
        py = r[3] / 3.3058 if r[3] else 0
        print(f"   {r[6][:10]}  {r[0]:14s} {r[1]:4s}  "
              f"mask={r[2]:8s}  {r[3]:>6,.0f}㎡({py:>4,.0f}평)  "
              f"{r[4]:>6,}만원  {r[5]:>5,.0f}만/평")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
