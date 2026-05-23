"""
rematch.py — trades.db의 모든 거래를 단계적 매칭으로 재매칭.

핵심 변경:
  1) 리(里) prefix10 매핑 자동 구축 — 거래 umd_name의 리 이름으로 좁힘
  2) 1차 매칭: 같은 리 + area_tol 2% (엄격)
  3) 2차 매칭: 1차 실패 시 같은 리 + area_tol 7% (완화)
  4) 리 정보 없으면 prefix8 (읍·면) 단위 fallback

V-World API 호출 없이 trades 테이블의 매칭 결과만 갱신.
"""

import json
import os
import sqlite3
import time
from collections import Counter, defaultdict

from jibun_matcher import Trade, Parcel, match_trade


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
REGION_CACHE = os.path.join(HERE, "region_prefix_cache.json")


def load_emd_to_prefix8():
    """region_prefix_cache.json에서 읍·면·동명 → prefix8 매핑 추출."""
    if not os.path.exists(REGION_CACHE):
        return {}
    with open(REGION_CACHE, encoding='utf-8') as f:
        d = json.load(f)
    return {emd: info['prefix8'] for emd, info in d.get('emd_map', {}).items()}


def extract_emd(umd_name):
    """거래 umd_name에서 첫 토큰(읍·면·동) 추출.
    '백암면 근삼리' → '백암면'."""
    if not umd_name:
        return None
    for tok in umd_name.split():
        if tok.endswith(('읍', '면', '동')):
            return tok
    return None

AREA_TOL_STRICT = 0.02   # 1차 — 엄격 (high)
AREA_TOL_LOOSE = 0.07    # 2차 — 신고면적 vs 지적면적 차이 흡수 (high 허용)
AREA_TOL_RECOVER = 0.10  # 3차 — 회수용 (단독이라도 mid로 강등; 신뢰 낮음)
MID_MAX = 10
RECOVER_MID_MAX = 10     # 3차에서 mid로 분류 가능한 최대 후보 수


# =====================================================================
#  리(里) 이름 ↔ prefix10 매핑
# =====================================================================
def build_ri_prefix_map(conn):
    """parcels의 PNU·addr에서 리 ↔ prefix10 매핑 자동 구축."""
    ri_to_prefix10 = {}
    seen_prefix = set()
    for pnu, addr in conn.execute(
        "SELECT pnu, addr FROM parcels WHERE addr IS NOT NULL AND addr != ''"
    ):
        prefix10 = pnu[:10]
        if prefix10 in seen_prefix:
            continue
        seen_prefix.add(prefix10)
        parts = (addr or '').split()
        # "경기도 용인시 처인구 원삼면 두창리 산 16" → "두창리" 추출
        for i, tok in enumerate(parts):
            if tok.endswith(('면', '읍', '동')) and i + 1 < len(parts):
                nxt = parts[i + 1]
                if nxt and not nxt[0].isdigit() and nxt != '산':
                    if nxt not in ri_to_prefix10:
                        ri_to_prefix10[nxt] = prefix10
                break
    return ri_to_prefix10


def extract_ri(umd_name):
    """거래 umd_name에서 리 이름 추출. '원삼면 두창리' → '두창리'."""
    if not umd_name:
        return None
    parts = umd_name.split()
    for i, tok in enumerate(parts):
        if tok.endswith(('면', '읍', '동')) and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt and not nxt[0].isdigit() and nxt != '산':
                return nxt
            break
    return None


# =====================================================================
#  parcels 로드 + 인덱스
# =====================================================================
def load_parcels_and_jiga(conn):
    parcels = []
    jiga_map = {}
    for row in conn.execute(
        "SELECT pnu, jibun, jimok, area_m2, lon, lat, jiga FROM parcels"
    ):
        parcels.append(Parcel(
            pnu=row[0], jibun=row[1] or '', jimok=row[2] or '',
            area_m2=row[3] or 0, lon=row[4] or 0, lat=row[5] or 0,
        ))
        jiga_map[row[0]] = row[6] or 0
    return parcels, jiga_map


def index_by_prefix_jimok(parcels, prefix_len):
    idx = defaultdict(list)
    for p in parcels:
        idx[(p.pnu[:prefix_len], p.jimok)].append(p)
    return idx


# =====================================================================
#  단계적 매칭
# =====================================================================
def _recover_match(trade, pool):
    """3차: ±10% 회수 단계. 단독이라도 high가 아닌 mid로 강등.
    단, 단독일 때 resolved는 유지 — 사용자 결과 목록에 후보 표시.
    시세 평균은 search.py에서 high만 쓰므로 mid라도 안전.
    """
    r = match_trade(trade, pool,
                    area_tol=AREA_TOL_RECOVER, mid_max=RECOVER_MID_MAX)
    if r.confidence == 'high':
        r.confidence = 'mid'  # 신뢰 강등 — resolved는 유지
    return r


def _try_three_steps(trade, pool):
    """주어진 후보 풀에 대해 1차(엄격)→2차(완화)→3차(회수) 순차 시도."""
    if not pool:
        return None
    r1 = match_trade(trade, pool,
                     area_tol=AREA_TOL_STRICT, mid_max=MID_MAX)
    if r1.confidence == 'high':
        return r1
    r2 = match_trade(trade, pool,
                     area_tol=AREA_TOL_LOOSE, mid_max=MID_MAX)
    if r2.confidence == 'high':
        return r2
    if r2.confidence == 'mid':
        return r2
    r3 = _recover_match(trade, pool)
    if r3.confidence == 'mid':
        return r3
    return r1  # 모두 실패 → 원래 low


def stepwise_match(trade, ri, ri_prefix10, idx10, idx8, emd, emd_prefix8):
    target10 = ri_prefix10.get(ri) if ri else None

    # 1) 리(prefix10) 한정 — 가장 좁고 가장 정확
    if target10:
        result = _try_three_steps(trade, idx10.get((target10, trade.jimok), []))
        if result and result.confidence != 'low':
            return result
        # 리 한정에서 low면 그대로 종료 (prefix8까지 확대하면 다른 리 케이스 끌어와 노이즈)
        if result:
            return result

    # 2) 리 매핑 실패 시 — emd 단위 prefix8 fallback (region_prefix_cache 활용)
    target8 = emd_prefix8.get(emd) if emd else None
    if target8:
        pool8 = idx8.get((target8, trade.jimok), [])
        result = _try_three_steps(trade, pool8)
        if result:
            return result

    # 3) 모든 매핑 실패 — low 반환
    from jibun_matcher import MatchResult
    return MatchResult(trade, 'low', [], None)


# =====================================================================
#  메인
# =====================================================================
def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 80)
    print(" rematch — 단계적 매칭으로 trades.db 재매칭")
    print("=" * 80)

    print("\n[1] 리(里) prefix10 매핑 구축...")
    ri_prefix10 = build_ri_prefix_map(conn)
    print(f"   {len(ri_prefix10)}개 리 매핑 완료")
    sample_keys = list(ri_prefix10.keys())[:6]
    for k in sample_keys:
        print(f"     {k}: {ri_prefix10[k]}")

    print("\n[2] parcels 로드 + 인덱스 구축...")
    t0 = time.time()
    parcels, jiga_map = load_parcels_and_jiga(conn)
    idx10 = index_by_prefix_jimok(parcels, 10)
    idx8 = index_by_prefix_jimok(parcels, 8)
    emd_prefix8 = load_emd_to_prefix8()
    print(f"   {len(parcels):,}필지  "
          f"idx10 키 {len(idx10):,}  idx8 키 {len(idx8):,}  "
          f"emd→prefix8 {len(emd_prefix8)}개  "
          f"({time.time() - t0:.1f}초)")

    # 기존 상태 통계 (비교용)
    old_summary = Counter()
    for row in conn.execute("SELECT match_confidence FROM trades"):
        old_summary[row[0] or 'null'] += 1

    print("\n[3] 모든 trades 재매칭...")
    trade_rows = list(conn.execute(
        "SELECT id, umd_name, jimok, area_m2, jibun_masked, is_san, "
        "deal_amount, deal_ymd, resolved_pnu FROM trades"
    ))
    print(f"   {len(trade_rows):,}건 처리 중...")

    t0 = time.time()
    new_summary = Counter()
    changed = Counter()  # (old_conf, new_conf) 변화 추적
    updates = []
    for row in trade_rows:
        tid, umd, jimok, area, jibun, is_san_int, price, dymd, old_pnu = row
        is_san = bool(is_san_int)
        masked = jibun[1:].strip() if is_san and jibun and jibun.startswith('산') else (jibun or '')

        trade = Trade(
            lawd_cd='', emd_name=umd or '', jimok=jimok or '',
            area_m2=area or 0,
            bonbeon_masked=masked, deal_price=price or 0,
            deal_ymd=dymd or '', is_san=is_san,
        )
        ri = extract_ri(umd)
        emd = extract_emd(umd)
        result = stepwise_match(trade, ri, ri_prefix10, idx10, idx8,
                                emd, emd_prefix8)
        new_summary[result.confidence] += 1

        new_pnu = result.resolved.pnu if result.resolved else None
        if (old_pnu or None) != (new_pnu or None):
            changed["pnu_changed"] += 1

        new_jibun = result.resolved.jibun if result.resolved else None
        new_area = result.resolved.area_m2 if result.resolved else None
        new_lon = result.resolved.lon if result.resolved else None
        new_lat = result.resolved.lat if result.resolved else None
        new_jiga = jiga_map.get(new_pnu) if new_pnu else None
        unit = None
        if new_area and new_area > 0:
            pyeong = new_area / 3.3058
            unit = (price or 0) / pyeong if pyeong > 0 else None

        updates.append((
            result.confidence, new_pnu, new_jibun, new_area,
            new_lon, new_lat, new_jiga, unit,
            len(result.candidates), tid,
        ))

    print(f"   매칭 완료 ({time.time() - t0:.1f}초)")

    print("\n[4] trades 테이블 업데이트...")
    conn.executemany(
        "UPDATE trades SET "
        "match_confidence=?, resolved_pnu=?, resolved_jibun=?, "
        "resolved_area_m2=?, resolved_lon=?, resolved_lat=?, "
        "resolved_jiga=?, unit_per_pyeong=?, candidates_count=? "
        "WHERE id=?",
        updates,
    )
    conn.commit()
    print(f"   {len(updates):,}건 업데이트  PNU 변경 {changed['pnu_changed']:,}건")

    print("\n[5] Before/After 매칭 분포")
    print("-" * 80)
    print(f"   {'':5s}  {'Before':>10s}  {'After':>10s}  {'Δ':>10s}")
    for k in ('high', 'mid', 'low'):
        before = old_summary.get(k, 0)
        after = new_summary.get(k, 0)
        delta = after - before
        sign = '+' if delta > 0 else ''
        print(f"   {k:5s}  {before:>10,}  {after:>10,}  {sign}{delta:>+10,}")

    print("\n[6] 검증 케이스 확인")
    print("-" * 80)
    cases = [
        ("두창리 957-5 답 152㎡ 8/4",
         "umd_name LIKE '%두창리%' AND jimok='답' AND deal_ymd LIKE '2025-08-04%' AND area_m2=152"),
        ("두창리 산 16 임야 5554㎡ 5/8",
         "umd_name LIKE '%두창리%' AND jimok='임야' AND deal_ymd LIKE '2024-05-08%' AND area_m2 BETWEEN 5553 AND 5555"),
        ("두창리 산 12 임야 6248㎡ 4/7",
         "umd_name LIKE '%두창리%' AND jimok='임야' AND deal_ymd LIKE '2023-04-07%' AND area_m2 BETWEEN 6247 AND 6249"),
    ]
    for name, where in cases:
        rows = list(conn.execute(
            f"SELECT umd_name, jibun_masked, area_m2, resolved_jibun, "
            f"match_confidence, candidates_count FROM trades WHERE {where}"
        ))
        if not rows:
            print(f"   {name}: 거래 없음")
            continue
        for r in rows:
            print(f"   {name}")
            print(f"     원본 {r[1]:8s}  면적 {r[2]:>7,.0f}㎡  →  "
                  f"복원 {r[3] or '-':14s}  [{r[4]}, 후보 {r[5]}개]")

    conn.close()
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
