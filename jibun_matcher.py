"""
================================================================================
 토지 실거래가 지번 복원 매칭 엔진 (프로토타입)
--------------------------------------------------------------------------------
 목표: 국토부 토지 실거래가의 "별표 지번"(예: 원삼면 산 5**)을
       V-World 연속지적도 필지 데이터와 면적·지목·본번대로 대조하여
       정확한 지번 + 좌표로 복원한다.

 데이터 소스
 -----------
 1) 국토부 토지 매매 실거래가 API (data.go.kr/data/15126466)
    - 입력: LAWD_CD(법정동 5자리) + DEAL_YMD(계약년월 6자리) + serviceKey
    - 출력: 거래별 [법정동, 지목, 거래면적, 본번(별표), 부번, 거래금액, 계약일]
 2) V-World 연속지적도 LP_PA_CBND_BUBUN (api.vworld.kr/req/data)
    - 입력: pnu:like:{법정동코드10자리} 등 attrFilter + key
    - 출력: GeoJSON. 필지별 [PNU(19자리), 지번, 지목, 면적(jibun/area), 경계좌표]
    - PNU 구조(19): 시도2 + 시군구3 + 읍면동3 + 리2 + 산구분1 + 본번4 + 부번4

 매칭 전략
 ---------
   거래(법정동+지목+면적+본번대)  ─┐
                                   ├─►  면적·지목·본번대 교차 매칭  ─► 후보 필지
   지적(법정동 전체 필지+좌표)   ─┘
   - 후보가 1개  → 확정 복원 (신뢰도 high)
   - 후보가 2~3개 → 부분 복원 (사용자 확인 필요, 신뢰도 mid)
   - 후보가 0개/다수 → 복원 실패 (법정동 단위로만 표시, 신뢰도 low)

 ⚠ API 키 없이도 로직 검증이 가능하도록 아래는 모의(mock) 데이터로 동작한다.
    실제 운영 시 fetch_molit_trades() / fetch_vworld_parcels() 의 내부를
    requests 호출로 교체하면 그대로 실데이터에 붙는다 (스텁 주석 참조).
================================================================================
"""

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
#  데이터 구조
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    """국토부 실거래 1건 (지번 일부 별표)"""
    lawd_cd: str          # 법정동 10자리 (시도2+시군구3+읍면동3+리2)
    emd_name: str         # 읍면동명 (표시용)
    jimok: str            # 지목 (임야/전/답/대 ...)
    area_m2: float        # 거래 토지면적 (㎡)
    bonbeon_masked: str   # 본번 별표 표기 (예: "5**", "12**")
    deal_price: int       # 거래금액 (만원)
    deal_ymd: str         # 계약년월 (YYYY-MM)
    is_san: bool = False  # 산(임야대장) 여부


@dataclass
class Parcel:
    """V-World 연속지적도 필지 1건 (정확 지번 + 좌표)"""
    pnu: str              # 19자리 PNU
    jibun: str            # 정확한 지번 (예: "산 542-3")
    jimok: str
    area_m2: float
    lon: float            # 대표점 경도
    lat: float            # 대표점 위도


@dataclass
class MatchResult:
    trade: Trade
    confidence: str                      # high / mid / low
    candidates: list = field(default_factory=list)  # list[Parcel]
    resolved: Optional[Parcel] = None    # 확정된 필지 (high일 때)


# ---------------------------------------------------------------------------
#  본번대 추출: "5**" -> (500, 599),  "12**" -> (1200, 1299)
#  국토부는 본번만 별표처리(부번 비공개)하며 뒤 2자리를 가린다.
# ---------------------------------------------------------------------------
def bonbeon_range(masked: str):
    digits = masked.replace("*", "")
    star_count = masked.count("*")
    if not digits:                       # 전부 별표인 경우 → 범위 특정 불가
        return None
    base = int(digits) * (10 ** star_count)
    span = (10 ** star_count) - 1
    return (base, base + span)


def pnu_bonbeon(pnu: str) -> int:
    """PNU에서 본번 추출 → 정수.
    PNU 끝 8자리는 항상 본번4+부번4. 뒤에서부터 세면 앞쪽
    법정동코드 자리수(10) 변화에 영향받지 않아 안전하다.
    """
    return int(pnu[-8:-4])


# ---------------------------------------------------------------------------
#  매칭 핵심 로직
# ---------------------------------------------------------------------------
def match_trade(trade: Trade, parcels: list, area_tol: float = 0.005,
                mid_max: int = 3) -> MatchResult:
    """
    거래 1건을 필지 목록과 대조.
    area_tol: 면적 허용오차 비율 (기본 0.5%). 지적 면적과 신고 면적의
              미세한 반올림 차이를 흡수하기 위함.
    mid_max:  '후보다수(mid)'로 분류할 최대 후보 수.
              그보다 많으면 low(실패)로 처리. 별표가 통째로 가려진(`*`)
              실거래에선 후보가 4~10개로 나오기 쉬워, 이 값을 늘리면
              유용한 후보 정보를 보존할 수 있음.
    """
    br = bonbeon_range(trade.bonbeon_masked)
    cands = []
    for p in parcels:
        # 1) 지목 일치
        if p.jimok != trade.jimok:
            continue
        # 2) 본번대 일치 (별표가 특정 가능한 경우만)
        if br is not None:
            bb = pnu_bonbeon(p.pnu)
            if not (br[0] <= bb <= br[1]):
                continue
        # 3) 면적 일치 (허용오차 내)
        diff = abs(p.area_m2 - trade.area_m2) / trade.area_m2
        if diff > area_tol:
            continue
        cands.append((diff, p))

    cands.sort(key=lambda x: x[0])
    candidates = [p for _, p in cands]

    if len(candidates) == 1:
        return MatchResult(trade, "high", candidates, candidates[0])
    elif 2 <= len(candidates) <= mid_max:
        return MatchResult(trade, "mid", candidates, None)
    else:
        return MatchResult(trade, "low", candidates, None)


# ---------------------------------------------------------------------------
#  실데이터 연동 스텁 (운영 시 내부를 requests 호출로 교체)
# ---------------------------------------------------------------------------
def fetch_molit_trades(lawd_cd5: str, deal_ymd: str, service_key: str) -> list:
    """
    국토부 토지 매매 실거래가 조회 (스텁).
    실제:
        url = "https://apis.data.go.kr/1613000/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade"
        params = {"serviceKey": service_key, "LAWD_CD": lawd_cd5,
                  "DEAL_YMD": deal_ymd, "numOfRows": 1000}
        xml = requests.get(url, params=params).text  # → XML 파싱하여 Trade 리스트로
    """
    raise NotImplementedError("운영 시 requests 호출로 교체")


def fetch_vworld_parcels(lawd_cd10: str, vworld_key: str) -> list:
    """
    V-World 연속지적도 필지 조회 (스텁).
    실제:
        url = "https://api.vworld.kr/req/data"
        params = {"service":"data","request":"GetFeature","data":"LP_PA_CBND_BUBUN",
                  "key":vworld_key,"attrFilter":f"pnu:like:{lawd_cd10}",
                  "page":1,"size":1000,"geometry":"true","format":"json"}
        geojson = requests.get(url, params=params).json()  # → features → Parcel 리스트
    """
    raise NotImplementedError("운영 시 requests 호출로 교체")


# ===========================================================================
#  데모: 모의 데이터로 매칭 엔진 검증
#  시나리오 — 용인시 처인구 원삼면(법정동코드 4146132026) 임야 거래
# ===========================================================================
def demo():
    LAWD = "4146132026"  # 용인 처인구 원삼면 (예시 코드)

    # --- 국토부에서 받았다고 가정한 거래 (본번 별표) ---
    trades = [
        Trade(LAWD, "원삼면", "임야", 1983.0, "5**",  41000, "2025-11", is_san=True),
        Trade(LAWD, "원삼면", "임야", 3471.0, "12**", 62000, "2025-09", is_san=True),
        Trade(LAWD, "원삼면", "임야", 992.0,  "3**",  23500, "2026-01", is_san=True),
        Trade(LAWD, "원삼면", "전",   1320.0, "8**",  95000, "2025-12"),
    ]

    # --- V-World에서 받았다고 가정한 원삼면 필지 목록 (정확 지번+좌표) ---
    #     표준 PNU(19) = 법정동코드10 + 산구분1 + 본번4 + 부번4
    parcels = [
        Parcel(LAWD + "1" + "0542" + "0003", "산 542-3", "임야", 1983.0, 127.331, 37.142),
        Parcel(LAWD + "1" + "0544" + "0001", "산 544-1", "임야", 5200.0, 127.335, 37.139),
        Parcel(LAWD + "1" + "1201" + "0007", "산 1201-7","임야", 3471.0, 127.341, 37.151),
        Parcel(LAWD + "1" + "0301" + "0002", "산 301-2", "임야", 990.0,  127.322, 37.160),
        Parcel(LAWD + "1" + "0305" + "0001", "산 305-1", "임야", 994.0,  127.324, 37.161),
        Parcel(LAWD + "0" + "0810" + "0004", "810-4",    "전",   1320.0, 127.318, 37.155),
    ]

    print("=" * 74)
    print(" 토지 실거래가 지번 복원 매칭 엔진 — 데모 (원삼면 임야)")
    print("=" * 74)

    summary = {"high": 0, "mid": 0, "low": 0}
    for t in trades:
        r = match_trade(t, parcels)
        summary[r.confidence] += 1
        tag = {"high": "✓ 확정", "mid": "△ 후보다수", "low": "✗ 실패"}[r.confidence]
        san = "산 " if t.is_san else ""
        print(f"\n[{tag}]  {t.emd_name} {san}{t.bonbeon_masked}  "
              f"{t.jimok} {t.area_m2:,.0f}㎡  {t.deal_price:,}만원 ({t.deal_ymd})")
        if r.resolved:
            p = r.resolved
            pyeong = p.area_m2 / 3.3058
            unit = t.deal_price / pyeong
            print(f"        └─ 복원 지번: {p.jibun}  (PNU {p.pnu})")
            print(f"           좌표: ({p.lat:.3f}, {p.lon:.3f})  "
                  f"평단가 약 {unit:,.0f}만원/평")
        elif r.candidates:
            names = ", ".join(f"{c.jibun}({c.area_m2:,.0f}㎡)" for c in r.candidates)
            print(f"        └─ 후보 {len(r.candidates)}개: {names}")
            print(f"           → 면적이 비슷한 필지가 여러 개라 자동확정 보류")
        else:
            print(f"        └─ 일치 필지 없음 → 원삼면 단위로만 표시")

    print("\n" + "-" * 74)
    print(f" 결과 요약:  확정 {summary['high']}건 · "
          f"후보다수 {summary['mid']}건 · 실패 {summary['low']}건")
    print(f" 자동 복원율: {summary['high']}/{len(trades)} = "
          f"{summary['high']/len(trades)*100:.0f}%")
    print("-" * 74)


if __name__ == "__main__":
    demo()
