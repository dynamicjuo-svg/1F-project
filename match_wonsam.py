"""
원삼면 실데이터 매칭 v2 — mid_max 확장 + 공시지가 기반 2차 매칭.

추가된 것:
  · V-World 응답을 wonsam_cache.json 에 캐싱 (두 번째 실행부터 1초 이내)
  · match_trade(mid_max=10) — 후보 4~10개도 mid 로 보존
  · 2차 매칭: mid 후보들을 거래금액/공시지가 비율로 정렬해 1순위 추천 도출
"""

import json
import os
import statistics
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from math import cos, radians

import requests

from api_keys import MOLIT_KEY, VWORLD_KEY
from jibun_matcher import Trade, Parcel, MatchResult, match_trade


CACHE_PATH = os.path.join(os.path.dirname(__file__), "wonsam_cache.json")
LAT_DEG_TO_M = 111049.0


# ============================================================================
#  1. 국토부 거래 → Trade
# ============================================================================
def fetch_molit_trades(lawd_cd5: str, deal_ymd: str) -> list:
    r = requests.get(
        "https://apis.data.go.kr/1613000/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade",
        params={
            "serviceKey": MOLIT_KEY, "LAWD_CD": lawd_cd5,
            "DEAL_YMD": deal_ymd, "numOfRows": "1000", "pageNo": "1",
        },
        timeout=30,
    )
    root = ET.fromstring(r.text)
    out = []
    for it in root.findall(".//item"):
        umd = (it.findtext("umdNm") or "").strip()
        jimok = (it.findtext("jimok") or "").strip()
        area_s = (it.findtext("dealArea") or "0").strip().replace(",", "")
        area = float(area_s) if area_s else 0.0
        jibun = (it.findtext("jibun") or "").strip()
        price_s = (it.findtext("dealAmount") or "0").strip().replace(",", "")
        price = int(price_s) if price_s.lstrip("-").isdigit() else 0
        y = (it.findtext("dealYear") or "").strip()
        m = (it.findtext("dealMonth") or "").strip()
        is_san = jibun.startswith("산")
        masked = jibun[1:].strip() if is_san else jibun
        out.append(Trade(
            lawd_cd="", emd_name=umd, jimok=jimok, area_m2=area,
            bonbeon_masked=masked, deal_price=price,
            deal_ymd=f"{y}-{m.zfill(2)}", is_san=is_san,
        ))
    return out


# ============================================================================
#  2. V-World 필지 → Parcel + jiga_map
# ============================================================================
JIMOK_MAP = {
    "전": "전", "답": "답", "과": "과수원", "목": "목장용지",
    "임": "임야", "광": "광천지", "염": "염전", "대": "대",
    "장": "공장용지", "학": "학교용지", "차": "주차장",
    "주": "주유소용지", "창": "창고용지", "도": "도로",
    "철": "철도용지", "제": "제방", "천": "하천", "구": "구거",
    "유": "유지", "양": "양어장", "수": "수도용지",
    "공": "공원", "체": "체육용지", "원": "유원지",
    "종": "종교용지", "사": "사적지", "묘": "묘지", "잡": "잡종지",
}


def _is_lonlat(point) -> bool:
    x, y = point[0], point[1]
    return 100 < x < 160 and 20 < y < 50


def ring_area_m2(ring):
    if not ring:
        return 0.0
    if _is_lonlat(ring[0]):
        avg_lat = sum(p[1] for p in ring) / len(ring)
        lon_m = LAT_DEG_TO_M * cos(radians(avg_lat))
        lat_m = LAT_DEG_TO_M
        s = 0.0
        n = len(ring)
        for i in range(n):
            x1 = ring[i][0] * lon_m
            y1 = ring[i][1] * lat_m
            x2 = ring[(i + 1) % n][0] * lon_m
            y2 = ring[(i + 1) % n][1] * lat_m
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_total(coords, geom_type):
    polys = coords if geom_type == "MultiPolygon" else [coords]
    total = 0.0
    first_outer = None
    for poly in polys:
        outer = poly[0]
        if first_outer is None:
            first_outer = outer
        total += ring_area_m2(outer)
        for hole in poly[1:]:
            total -= ring_area_m2(hole)
    cx = sum(p[0] for p in first_outer) / len(first_outer)
    cy = sum(p[1] for p in first_outer) / len(first_outer)
    return total, cx, cy


def extract_jimok_char(jibun, bubun):
    for s in (jibun, bubun):
        s = (s or "").strip()
        if s and not s[-1].isdigit():
            return s[-1]
    return ""


def fetch_vworld_parcels(prefix8: str):
    parcels, jiga_map = [], {}
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
        if page == 1:
            total_rec = resp.get("record", {}).get("total", "?")
            page_total = int(resp.get("page", {}).get("total", "1"))
            print(f"   전체 {total_rec}필지, {page_total}페이지")
        feats = (resp.get("result", {}).get("featureCollection", {}) or {}).get("features", [])
        if not feats:
            break
        for ft in feats:
            p = ft["properties"]
            g = ft.get("geometry") or {}
            try:
                area_m2, cx, cy = polygon_total(g["coordinates"], g["type"])
            except Exception:
                area_m2, cx, cy = 0.0, 0.0, 0.0
            jibun = (p.get("jibun") or "").strip()
            bubun = (p.get("bubun") or "").strip()
            jimok_full = JIMOK_MAP.get(extract_jimok_char(jibun, bubun), "?")
            try:
                jiga = float(p.get("jiga") or 0)
            except (TypeError, ValueError):
                jiga = 0.0
            parcels.append(Parcel(
                pnu=p["pnu"], jibun=jibun, jimok=jimok_full,
                area_m2=area_m2, lon=cx, lat=cy,
            ))
            jiga_map[p["pnu"]] = jiga
        print(f"   page {page}/{page_total}  누적 {len(parcels):,}필지")
        if page >= page_total:
            break
        page += 1
        time.sleep(0.15)
    return parcels, jiga_map


def load_or_fetch_parcels(prefix8: str):
    """캐시 우선 — 매번 35페이지 받는 시간 낭비 방지."""
    if os.path.exists(CACHE_PATH):
        print(f"   [캐시 사용] {os.path.basename(CACHE_PATH)}")
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        parcels = [Parcel(
            pnu=d["pnu"], jibun=d["jibun"], jimok=d["jimok"],
            area_m2=d["area_m2"], lon=d["lon"], lat=d["lat"],
        ) for d in cache["parcels"]]
        jiga_map = {d["pnu"]: d.get("jiga", 0) for d in cache["parcels"]}
        return parcels, jiga_map

    print("   [캐시 없음] V-World에서 받아옵니다 (시간 좀 걸려요)")
    parcels, jiga_map = fetch_vworld_parcels(prefix8)
    serial = [{
        "pnu": p.pnu, "jibun": p.jibun, "jimok": p.jimok,
        "area_m2": p.area_m2, "lon": p.lon, "lat": p.lat,
        "jiga": jiga_map.get(p.pnu, 0),
    } for p in parcels]
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"prefix8": prefix8, "parcels": serial}, f, ensure_ascii=False)
    print(f"   [캐시 저장] {CACHE_PATH}")
    return parcels, jiga_map


# ============================================================================
#  3. 2차 매칭 — 거래금액/공시지가 비율로 mid 후보 정렬
# ============================================================================
def score_candidates(result: MatchResult, jiga_map: dict):
    """후보별 (공시지가총액 → 거래가/공시지가 비율) 계산 후 정렬.
    합리적 ratio 범위(0.3~30) 안에 있고 1.5~3에 가까운 것을 1순위로."""
    t = result.trade
    price_won = t.deal_price * 10000
    scored = []
    for p in result.candidates:
        jiga = jiga_map.get(p.pnu, 0)
        if jiga > 0 and p.area_m2 > 0:
            estimated = jiga * p.area_m2
            ratio = price_won / estimated
        else:
            ratio = None
        scored.append((p, ratio, jiga))

    def key(item):
        p, r, j = item
        if r is None:
            return (3, 0.0)
        if 0.3 <= r <= 30:
            # 1.5~3.0 부근 시세 비율을 가장 그럴듯한 것으로 점수화
            target = 2.0
            return (0, abs(r - target))
        return (1, abs(r))

    scored.sort(key=key)
    return scored


# ============================================================================
#  4. 공유지분 라벨링
# ============================================================================
def label_group(group, jimok_unit_median):
    """확정 매칭된 거래 그룹(같은 PNU)에 권리분석용 라벨 부여."""
    n = len(group)
    parcel = group[0].resolved
    prices = [r.trade.deal_price for r in group]
    areas = [r.trade.area_m2 for r in group]

    pyeong = parcel.area_m2 / 3.3058 if parcel.area_m2 > 0 else 0
    avg_price = sum(prices) / n
    unit_per_pyeong = avg_price / pyeong if pyeong > 0 else 0

    if n == 1:
        category = "단독"
    elif n <= 3:
        category = "공유지분"
    elif n <= 7:
        category = "다수공유"
    else:
        category = "대규모공유"

    flags = []
    if n >= 2:
        same_price_count = max(prices.count(p) for p in set(prices))
        if same_price_count / n >= 0.8:
            flags.append("동일금액")
        if max(areas) > 0 and (max(areas) - min(areas)) / max(areas) < 0.05:
            flags.append("동일면적")
    median = jimok_unit_median.get(parcel.jimok)
    if median and unit_per_pyeong > 0 and unit_per_pyeong < median * 0.5:
        flags.append("저평단가")

    return {
        "category": category,
        "n": n,
        "pnu": parcel.pnu,
        "jibun": parcel.jibun,
        "jimok": parcel.jimok,
        "area_m2": parcel.area_m2,
        "unit_per_pyeong": unit_per_pyeong,
        "median_for_jimok": median,
        "flags": flags,
        "group": group,
    }


# ============================================================================
#  5. 실행
# ============================================================================
print("=" * 80)
print(" 원삼면 매칭 v2 — mid_max 확장 + 공시지가 2차 매칭")
print("=" * 80)

print("\n[1] 국토부 거래 받는 중...")
trades_all = fetch_molit_trades("41461", "202511")
trades = [t for t in trades_all if "원삼면" in t.emd_name]
print(f"   처인구 전체 {len(trades_all)}건  →  원삼면 {len(trades)}건")

print("\n[2] V-World 필지 적재")
t0 = time.time()
parcels, jiga_map = load_or_fetch_parcels("41461340")
print(f"   {len(parcels):,}필지  ({time.time() - t0:.1f}초)")
print(f"   jiga(공시지가) 가진 필지: "
      f"{sum(1 for v in jiga_map.values() if v > 0):,}/{len(jiga_map):,}")

# 매칭 옵션 비교 — 기준 3 vs 10
print("\n[3] 매칭 두 설정 비교")
for mid_max in (3, 10):
    res = [match_trade(t, parcels, area_tol=0.02, mid_max=mid_max) for t in trades]
    s = Counter(r.confidence for r in res)
    print(f"   mid_max={mid_max:2d}  →  high {s['high']:3d}  "
          f"mid {s['mid']:3d}  low {s['low']:3d}")

# 본 매칭 (mid_max=10)
print("\n[4] 본 매칭 (mid_max=10, area_tol=2%)")
results = [match_trade(t, parcels, area_tol=0.02, mid_max=10) for t in trades]
summary = Counter(r.confidence for r in results)
N = len(results)
print(f"   확정(high) {summary['high']:3d}  "
      f"후보다수(mid) {summary['mid']:3d}  "
      f"실패(low) {summary['low']:3d}  /  총 {N}건")
print(f"   ▶ 자동 복원율: {summary['high']/N*100:.1f}%   "
      f"부분 복원율(high+mid): {(summary['high']+summary['mid'])/N*100:.1f}%")

# 2차 매칭으로 mid → 1순위 추천
print("\n[5] mid 후보에 공시지가 기반 추천 적용")
print("-" * 80)
recommended_count = 0
for r in results[:200]:
    if r.confidence != "mid":
        continue
    t = r.trade
    scored = score_candidates(r, jiga_map)
    if not scored:
        continue
    top_p, top_r, top_j = scored[0]
    if top_r is not None and 0.3 <= top_r <= 30:
        recommended_count += 1

print(f"   mid {summary['mid']}건 중, 공시지가 비율로 1순위 추천 도출 "
      f"가능한 케이스: {recommended_count}건")

# 추천 가능 mid 사례 5건 표시
print("\n[6] mid + 1순위 추천 미리보기 (5건)")
print("-" * 80)
shown = 0
for r in results:
    if r.confidence != "mid":
        continue
    scored = score_candidates(r, jiga_map)
    if not scored or scored[0][1] is None:
        continue
    t = r.trade
    san = "산 " if t.is_san else ""
    print(f"\n• {t.emd_name}  {san}{t.bonbeon_masked} "
          f"({t.jimok} {t.area_m2:,.0f}㎡, {t.deal_price:,}만원)")
    for rank, (p, ratio, jiga) in enumerate(scored, 1):
        marker = "★1순위" if rank == 1 else f"  {rank}순위"
        if ratio is not None:
            print(f"   {marker}  {p.jibun}  ({p.area_m2:,.0f}㎡)  "
                  f"공시지가 {jiga:,.0f}원/㎡  비율 {ratio:.2f}x")
        else:
            print(f"   {marker}  {p.jibun}  ({p.area_m2:,.0f}㎡)  "
                  f"공시지가 정보 없음")
    shown += 1
    if shown >= 5:
        break

# 확정 사례 5건도 같이 (변화 없음 확인용)
print(f"\n[7] 확정 사례 5건 (참고)")
print("-" * 80)
confirmed = [r for r in results if r.confidence == "high"]
for i, r in enumerate(confirmed[:5], 1):
    t, p = r.trade, r.resolved
    jiga = jiga_map.get(p.pnu, 0)
    pyeong = p.area_m2 / 3.3058
    unit_market = (t.deal_price / pyeong) if pyeong else 0
    ratio = (t.deal_price * 10000) / (jiga * p.area_m2) if jiga > 0 else None
    san = "산 " if t.is_san else ""
    ratio_s = f"  공시 대비 {ratio:.2f}x" if ratio else ""
    print(f" {i}. {t.emd_name} {san}{t.bonbeon_masked} → "
          f"{p.jibun} ({p.area_m2:,.0f}㎡)  "
          f"평단가 {unit_market:,.0f}만원/평{ratio_s}")

# 같은 PNU에 거래 그룹화 (확정 매칭만)
pnu_groups = defaultdict(list)
for r in results:
    if r.confidence == "high" and r.resolved:
        pnu_groups[r.resolved.pnu].append(r)

# 지목별 단독매매 평단가 중앙값 (공유지분 거래 왜곡 제외용)
solo_unit_by_jimok = defaultdict(list)
for pnu, lst in pnu_groups.items():
    if len(lst) == 1:
        r = lst[0]
        pyeong = r.resolved.area_m2 / 3.3058
        if pyeong > 0:
            solo_unit_by_jimok[r.trade.jimok].append(r.trade.deal_price / pyeong)
jimok_median = {k: statistics.median(v) for k, v in solo_unit_by_jimok.items() if v}

print(f"\n[8] 공유지분 라벨링 (확정 매칭 {summary['high']}건 기준)")
print("-" * 80)
print(f"   지목별 단독매매 평단가 중앙값 (시세 기준선)")
for k, v in jimok_median.items():
    print(f"     {k:6s} {v:>8,.0f}만원/평  (단독매매 {len(solo_unit_by_jimok[k])}건)")

# 라벨링
labeled = [label_group(lst, jimok_median) for lst in pnu_groups.values()]
cat_count = Counter(L["category"] for L in labeled)
print(f"\n   카테고리별 필지 수")
for c in ("단독", "공유지분", "다수공유", "대규모공유"):
    if cat_count[c]:
        # 그 카테고리에 속한 거래 건수도 함께
        n_trades = sum(L["n"] for L in labeled if L["category"] == c)
        print(f"     {c:8s}  필지 {cat_count[c]:3d}개  /  거래 {n_trades:3d}건")

# 공유지분 + 다수공유 + 대규모공유 (1건 초과) — 권리분석 주목 대상
shared = sorted(
    [L for L in labeled if L["category"] != "단독"],
    key=lambda x: -x["n"],
)
print(f"\n[9] 공유지분 거래 필지 상세 ({len(shared)}건)")
print("-" * 80)
for L in shared:
    median = L["median_for_jimok"]
    flags_s = "  " + " ".join(f"[{f}]" for f in L["flags"]) if L["flags"] else ""
    median_s = f"  (시세중앙값 {median:,.0f}만원/평)" if median else ""
    print(f"\n  ▣ {L['jibun']}  {L['jimok']} {L['area_m2']:,.0f}㎡  "
          f"PNU {L['pnu']}")
    print(f"     [{L['category']}]  {L['n']}인 공유  "
          f"평단가 {L['unit_per_pyeong']:,.0f}만원/평{median_s}{flags_s}")
    # 거래 명세 (최대 5건)
    for r in L["group"][:5]:
        t = r.trade
        print(f"       · {t.deal_ymd}  {t.area_m2:,.0f}㎡  {t.deal_price:,}만원")
    if len(L["group"]) > 5:
        print(f"       · ... 외 {len(L['group']) - 5}건")

print("\n" + "=" * 80)
print(" 라벨 해석 가이드:")
print("  · [동일금액]   80%+ 거래가 같은 금액 → 사전 분양 가격표 가능성")
print("  · [동일면적]   모든 거래 면적이 5% 이내 동일 → 균등 지분 분할")
print("  · [저평단가]   같은 지목 단독매매 시세 중앙값의 50% 미만")
print("  → 셋 다 붙은 필지는 권리분석 정밀 검토 권장")
print("=" * 80)
