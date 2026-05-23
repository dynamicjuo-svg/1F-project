"""
search.py v2 — 확장된 자연어 토지 실거래 검색.

지원하는 표현 (이전 + 확장):
  지역·도로·기간·반경·지목·면적 (v1)
  + 수치: "평당 100 미만", "1억 이하", "5천만~2억"
  + 절대 시간: "2024년", "2023년 상반기", "올해"
  + 필터: "단독매매만", "지분거래 제외", "임야 제외"
  + 정렬: "평단가 낮은 순", "면적 큰 순", "최근 거래순"

사용:
    python -X utf8 search.py
    python -X utf8 search.py "원삼면 임야 평당 100 미만 최근 1년"
"""

import json
import os
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from math import cos, radians

import anthropic
from api_keys import ANTHROPIC_KEY


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
REGION_CACHE = os.path.join(HERE, "region_prefix_cache.json")
MODEL = "claude-haiku-4-5-20251001"


# =====================================================================
#  자연어 파서 (확장된 스키마)
# =====================================================================
PARSER_SYSTEM = """당신은 한국 토지 실거래가 검색 시스템의 자연어 질의 파서입니다.
사용자 한 줄 질의를 JSON 객체로만 출력하세요.

스키마 (모든 키 포함, 없으면 null):
{
  "sigg": "시·군·구 (예: '용인시 처인구')",
  "emd_list": "읍·면·동 배열 (예: ['원삼면'] 또는 ['원삼면','백암면']). 한 곳이어도 배열로",
  "road_query": "도로 식별자 ('덕평로' 또는 '지방도318')",
  "radius_m": "도로 반경 m(정수). '5키로'='5km'=5000",
  "jimok_list": "지목 배열. 가능: 전,답,과수원,목장용지,임야,대,공장용지,창고용지,주차장,주유소용지,도로,철도용지,제방,하천,구거,유지,양어장,수도용지,공원,체육용지,유원지,종교용지,사적지,묘지,잡종지",
  "exclude_jimok_list": "제외할 지목 배열",
  "period_months": "최근 N개월 (현재 기준 역산). '1년간'→12, '최근 6개월'→6",
  "year_start": "절대 연도 시작(정수). '2024년'→2024",
  "year_end": "절대 연도 끝(정수)",
  "month_start": "월 시작(1~12). '상반기'→1, '하반기'→7",
  "month_end": "월 끝. '상반기'→6, '하반기'→12",
  "min_area_m2": "최소 면적 m². '1000평'→3306",
  "max_area_m2": "최대 면적 m²",
  "min_deal_amount": "최소 거래금액(만원). '1억'→10000, '5천만'→5000",
  "max_deal_amount": "최대 거래금액(만원)",
  "min_unit_per_pyeong": "최소 평단가(만원/평)",
  "max_unit_per_pyeong": "최대 평단가(만원/평)",
  "exclude_shared": "true=공유지분 제외(단독매매만), false/null=모두",

  "min_elevation_m": "해발 최소 (m). '해발 100 이상'→100, '고지대'→200",
  "max_elevation_m": "해발 최대 (m). '저지대'→50",
  "max_slope_deg": "경사 최대 (도). '평지'→5, '완만한'→15, '급경사 빼고'→25",
  "zone_include": "용도지역 포함 배열. 가능: 도시지역,관리지역,농림지역,자연환경보전지역,계획관리지역,생산관리지역,보전관리지역,주거지역,상업지역,공업지역,녹지지역",
  "zone_exclude": "용도지역 제외 배열",
  "exclude_gb": "true=개발제한구역(그린벨트) 제외",
  "exclude_protected_forest": "true=보전산지 제외",
  "exclude_farm_promote": "true=농업진흥구역 제외",
  "max_stream_dist_m": "하천 최대 거리(m). '하천옆'→200, '하천 100m 이내'→100, '하천에서 가까운'→300",
  "min_stream_dist_m": "하천 최소 거리(m). '하천에서 떨어진'→500",
  "require_road_access": "true=도로 접면 필수. '맹지 아닌'·'도로 접한'·'도로변'→true",
  "exclude_road_access": "true=맹지만. '맹지만'→true (드문 표현)",
  "exclude_flood": "true=침수예상지역 제외. '침수 빼고'·'안전한'→true",

  "shape_include": "토지 형상 포함 배열. 가능: '정방형','장방형','길쭉형','L자형','불규칙'. '정방형만'→['정방형'], '직사각형에 가까운'→['정방형','장방형']",
  "shape_exclude": "토지 형상 제외 배열. '길쭉한 모양 빼고'→['길쭉형'], 'L자형 빼고'→['L자형']",
  "reference_jibun": "특정 필지를 기준으로 '비슷한 조건' 검색할 때 그 필지 지번 (정규화 문자열). 예: '백암면 근삼리 산 75-1', '원삼면 두창리 957-5'. 표현: 'X와 비슷한 조건', 'X 같은 조건', 'X와 유사한 매물', 'X처럼 생긴 땅'. 시스템이 그 필지의 jimok/면적/해발/경사/맹지여부/형상을 자동 추출해 cond에 채움. 사용자가 명시한 cond는 덮어쓰지 않음.",
  "similarity_level": "'X와 비슷한' 검색의 폭 강도. 'strict'(면적±15% 해발±30m 경사+3°) | 'normal'(±30% ±50m +5°, 기본) | 'loose'(±50% ±80m +10°). 표현: '매우 비슷한'·'정확히 같은'→strict, '비슷한'·'유사한'→normal, '폭넓게 비슷한'·'대충 비슷한'·'널널하게'→loose.",
  "area_tol_pct": "참조 필지 면적 허용 폭 %. '면적 ±20%'·'면적 20프로 안'→20. similarity_level보다 우선.",
  "elevation_tol_m": "참조 필지 해발 허용 폭 m. '해발 ±30m'·'해발 비슷한 30m 안'→30. similarity_level보다 우선.",
  "slope_tol_deg": "참조 필지 경사 허용 추가 폭 도. '경사 ±3도'·'경사 거의 같은'→3. similarity_level보다 우선.",

  "sort_by": "정렬 기준: 'deal_ymd'|'unit_per_pyeong'|'area_m2'|'deal_amount'",
  "sort_order": "'asc'(낮은순/오래된순) 또는 'desc'(높은순/최근). 기본 'desc'"
}

규칙:
- "1억"=10000, "5천만"=5000, "2억5천"=25000 (만원 단위)
- "평당 100"·"평당 100만원" → 평단가 100만원/평
- "올해"·"이번해" → year_start=year_end=2025 (DB 최신)
- "작년" → year_start=year_end=2024
- "재작년" → year_start=year_end=2023
- "2024년 상반기" → year_start=year_end=2024, month_start=1, month_end=6
- "2023년 하반기" → year_start=year_end=2023, month_start=7, month_end=12
- "최근 N개월"·"최근 N년" → period_months=N개월 (N년=N*12, 절대 시간이 명시되면 그게 우선)
- **"N년치"**·**"N년간"**·**"N년동안"** → period_months=N*12 (예: "5년치"=60)
- "단독매매만"·"지분거래 제외" → exclude_shared=true
- "평단가 낮은 순" → sort_by=unit_per_pyeong, sort_order=asc
- "최근 거래순" → sort_by=deal_ymd, sort_order=desc
- "면적 큰 순"·"넓은 순" → sort_by=area_m2, sort_order=desc
- "비싼 순"·"고가 순" → sort_by=deal_amount, sort_order=desc
- "임야 제외" → exclude_jimok_list=['임야']
- "100평 이상" → min_area_m2=330 (= 100*3.3058 반올림)
- 1평 ≈ 3.3058㎡, 시군구가 안 명시되어도 읍면동으로 추정 가능하면 채울 것
- "5키로"·"5킬로"·"5km" → radius_m=5000
- "원삼면 백암면", "원삼면과 백암면", "두 면" 같이 여러 읍·면·동이 명시되면
  emd_list에 모두 넣을 것: ["원삼면", "백암면"]

**입지/규제 표현 예시 (토지 구매 체크리스트)**:
- "해발 100m 이상" → min_elevation_m=100
- "해발 50m 미만" → max_elevation_m=50
- "평지에 가까운"·"평평한" → max_slope_deg=5
- "경사 완만한"·"완경사" → max_slope_deg=15
- "급경사 빼고" → max_slope_deg=25
- "관리지역만"·"관리지역에서" → zone_include=["관리지역"]
- "계획관리지역" → zone_include=["계획관리지역"]
- "농림 빼고"·"농림지역 제외" → zone_exclude=["농림지역"]
- "도시지역 제외"·"비도시" → zone_exclude=["도시지역"]
- "그린벨트 빼고"·"개발제한구역 빼고"·"GB 빼고" → exclude_gb=true
- "보전산지 빼고"·"보산 빼고" → exclude_protected_forest=true
- "농업진흥구역 빼고"·"농진 빼고" → exclude_farm_promote=true
- "하천옆"·"강 옆"·"하천 가까운" → max_stream_dist_m=200
- "하천 100m 이내" → max_stream_dist_m=100
- "하천에서 떨어진"·"하천 멀리" → min_stream_dist_m=500
- "맹지 아닌"·"도로 접한"·"도로변"·"진입로 있는" → require_road_access=true
- "맹지만" → exclude_road_access=true
- "침수 빼고"·"침수예상 제외"·"안전한 곳" → exclude_flood=true
- 복합: "관리지역 해발 100m 이상 맹지 아닌" →
  zone_include=["관리지역"], min_elevation_m=100, require_road_access=true

**토지 형상 표현**:
- "정방형만"·"네모반듯한"·"정사각형 가까운" → shape_include=["정방형"]
- "직사각형"·"장방형" → shape_include=["장방형"]
- "정방형 또는 장방형"·"직사각형에 가까운" → shape_include=["정방형","장방형"]
- "길쭉한"·"길쭉하게 생긴" → shape_include=["길쭉형"]
- "길쭉한 모양 빼고" → shape_exclude=["길쭉형"]
- "L자 빼고"·"꺾인 모양 빼고" → shape_exclude=["L자형"]
- "불규칙 빼고"·"모양 좋은" → shape_exclude=["불규칙","L자형"]

**참조 필지(reference_jibun) 표현 — 'X와 비슷한 조건' 검색**:
- "근삼리 산75-1과 비슷한 조건의 실거래가" → reference_jibun="근삼리 산 75-1"
- "백암면 근삼리 산75-1 같은 조건" → reference_jibun="백암면 근삼리 산 75-1"
- "원삼면 두창리 957-5와 유사한 매물" → reference_jibun="원삼면 두창리 957-5"
- "산75-1처럼 생긴 임야" → reference_jibun="산 75-1", jimok_list=["임야"] (보강용)
- 정규화 규칙:
  · '산' 다음에 공백 한 칸 ('산75-1' → '산 75-1')
  · 본번-부번 사이 하이픈 그대로 ('75-1')
  · emd·ri가 입력에 있으면 포함, 없으면 지번만 ('산 75-1')
- 사용자가 reference_jibun 외에 다른 cond도 명시하면 그것은 그대로 두고
  reference에서 자동 채우는 부분은 시스템이 알아서 (사용자 cond 우선)

**비슷한 조건의 '폭' 조절 (similarity_level + 항목별 tol)**:
- "근삼리 산75-1과 매우 비슷한" → similarity_level="strict"
- "두창리 957-5와 정확히 같은 조건" → similarity_level="strict"
- "산75-1과 폭넓게 비슷한"·"대충 비슷한"·"널널하게" → similarity_level="loose"
- "X와 비슷한데 면적 ±20%" → reference_jibun=X, area_tol_pct=20
- "X 같은데 해발 ±30m 안" → reference_jibun=X, elevation_tol_m=30
- "X 같은 조건 경사 거의 같은" → reference_jibun=X, slope_tol_deg=3
- 우선순위: 사용자가 직접 min_area_m2 등 명시 > 항목별 *_tol > similarity_level > 기본

**복합 명령 처리** (한 문장에 여러 조건이 순차적으로 등장하는 경우):
- "찾아보고", "찾아봐", "보여줘", "알려줘" 등 검색 동사는 무시
- "이중에", "그중에", "그 중에서", "거기서", "그것중", "여기서" 등 부분 한정 표현은
  앞뒤 조건을 **모두 합쳐** 단일 검색으로 처리 (AND 조건)
- 예: "백암면 100평 이상 5년치 거래 찾아보고 이중에 임야인걸 찾아봐"
  → emd='백암면', min_area_m2=330, period_months=60, jimok_list=['임야'] (모두 AND)
- 예: "원삼면 임야 거래 보여줘, 그 중에서 평당 100 미만만"
  → emd='원삼면', jimok_list=['임야'], max_unit_per_pyeong=100

추정 못 하는 필드는 null."""


ROAD_MAPPER_SYSTEM = """당신은 한국 도로 매핑 전문가입니다.
사용자가 입력한 도로 식별자에 가장 잘 맞는 도로명을 후보 목록에서 고르세요.

응답은 JSON 객체로만:
{"matched": "선택한 도로명 또는 null", "confidence": "high|mid|low", "reason": "한 문장"}

도로번호 매핑 지식 참고:
- 지방도318: 경기도 안성-원삼-양지 → '덕평로' 등
- 지방도325: 안성 일대
- 일반국도42: 인천-수원-이천
- 일반국도17: 수원-용인-안양"""


def parse_query(client, query: str) -> dict:
    msg = client.messages.create(
        model=MODEL, max_tokens=600, system=PARSER_SYSTEM,
        messages=[
            {"role": "user", "content": query},
            {"role": "assistant", "content": "{"},
        ],
    )
    text = "{" + msg.content[0].text
    # LLM이 가끔 여러 JSON 객체나 뒤에 설명문을 붙임 → 첫 완전 객체만 추출
    depth = 0
    end_idx = len(text)
    for i, c in enumerate(text):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    cond = json.loads(text[:end_idx])
    # 하위 호환: 옛 'emd' 키만 채워졌으면 emd_list로 정규화
    if cond.get("emd") and not cond.get("emd_list"):
        cond["emd_list"] = [cond["emd"]]
    return cond


# 도로번호 → 도로명 리스트 내장 매핑 (위키 등 외부 자료 기반)
# 일치하면 LLM 추측보다 우선 사용
ROAD_NUMBER_TO_NAMES = {
    # 지방도 제318호선: 화성 ~ 용인 처인구(이동읍·원삼면) ~ 이천 ~ 장호원
    "지방도318": [
        "백자로", "백옥대로", "이원로",         # 이동읍 구간
        "보개원삼로", "백원로", "원설로",        # 원삼면 구간
    ],
    # 필요시 다른 노선 추가
}


def _norm_road_query(q: str) -> str:
    """'지방도 제318호선' / '지방도318호' / '318지방도' 등을 같은 키로 정규화."""
    s = (q or "").replace(" ", "").replace("호선", "").replace("호", "")
    # '318지방도' → '지방도318'로 통일
    import re
    m = re.match(r"^(\d+)(지방도|국도|국가지원지방도)$", s)
    if m:
        s = m.group(2) + m.group(1)
    m = re.match(r"^제?(\d+)$", s)  # 제318 또는 그냥 318
    return s


def map_road(client, road_query, candidates):
    # 1) 내장 매핑 사전 우선
    norm = _norm_road_query(road_query)
    for key, names in ROAD_NUMBER_TO_NAMES.items():
        if _norm_road_query(key) == norm:
            return {
                "matched": names,  # 리스트
                "confidence": "exact",
                "reason": f"내장 노선 매핑 ({len(names)}개 도로)",
            }

    # 2) 없으면 LLM에 단일 후보 매핑 요청
    if not candidates:
        return {"matched": None, "confidence": "low"}
    cand_text = ", ".join(f'"{c}"' for c in candidates[:80])
    msg = client.messages.create(
        model=MODEL, max_tokens=300, system=ROAD_MAPPER_SYSTEM,
        messages=[
            {"role": "user", "content": f"입력: '{road_query}'\n후보: [{cand_text}]"},
            {"role": "assistant", "content": "{"},
        ],
    )
    try:
        return json.loads("{" + msg.content[0].text)
    except json.JSONDecodeError:
        return {"matched": None, "confidence": "low"}


# =====================================================================
#  reference_jibun — 참조 필지 lookup + cond 자동 채움
# =====================================================================
def _load_emd_to_prefix8():
    cache_path = os.path.join(HERE, "region_prefix_cache.json")
    if not os.path.exists(cache_path):
        return {}
    with open(cache_path, encoding="utf-8") as f:
        d = json.load(f)
    return {emd: info["prefix8"] for emd, info in d.get("emd_map", {}).items()}


_EMD_P8 = None


def _parse_reference_str(ref_str):
    """'백암면 근삼리 산 75-1' → (emd, ri, jibun_norm).
    emd/ri는 입력에 없으면 None.
    jibun_norm은 '산 75-1' 또는 '957-5' 형태로 공백 정규화."""
    if not ref_str:
        return None, None, None
    s = ref_str.strip()
    parts = s.split()
    emd = None
    ri = None
    rest_start = 0
    for i, tok in enumerate(parts):
        if tok.endswith(("읍", "면", "동")):
            emd = tok
            rest_start = i + 1
        elif tok.endswith("리"):
            ri = tok
            rest_start = i + 1
    rest = " ".join(parts[rest_start:]).strip()
    # 산 정규화: '산75-1' → '산 75-1'
    if rest.startswith("산") and len(rest) > 1 and rest[1] != " ":
        rest = "산 " + rest[1:]
    # 공백 정리
    rest = " ".join(rest.split())
    return emd, ri, rest


def _candidate_jibun_patterns(jibun_norm):
    """parcels.jibun은 '산 75-1임' / '942-7답' 처럼 끝에 jimok 한 글자가 붙음.
    매칭 패턴 두 가지 시도 — 공백 포함/제거."""
    base = jibun_norm.strip()
    nospace = base.replace(" ", "")
    return [base + "%", nospace + "%"]


def _prefix8_from_ri_via_trades(conn, ri):
    """trades.umd_name LIKE '%{ri}%' 매칭 거래의 resolved_pnu prefix8 최빈값.
    백암면 parcels.addr 누락 이슈를 trades 데이터로 우회.
    """
    row = conn.execute(
        "SELECT SUBSTR(resolved_pnu, 1, 8) p8, COUNT(*) cnt "
        "FROM trades WHERE umd_name LIKE ? AND resolved_pnu IS NOT NULL "
        "GROUP BY p8 ORDER BY 2 DESC LIMIT 1",
        ('%' + ri + '%',)
    ).fetchone()
    return row[0] if row else None


def lookup_reference_parcel(conn, ref_str):
    """reference_jibun 문자열 → 최적 단일 필지 row (dict).
    못 찾으면 None."""
    global _EMD_P8
    if _EMD_P8 is None:
        _EMD_P8 = _load_emd_to_prefix8()
    emd, ri, jibun_norm = _parse_reference_str(ref_str)
    if not jibun_norm:
        return None

    # emd 우선, 없으면 ri를 trades 거래로 역추적
    target_p8 = None
    if emd:
        target_p8 = _EMD_P8.get(emd)
    if not target_p8 and ri:
        target_p8 = _prefix8_from_ri_via_trades(conn, ri)

    where = []
    params = []
    if target_p8:
        where.append("prefix8 = ?")
        params.append(target_p8)
    # jibun LIKE 패턴 OR
    patterns = _candidate_jibun_patterns(jibun_norm)
    where.append("(" + " OR ".join("jibun LIKE ?" for _ in patterns) + ")")
    params += patterns
    sql = (
        "SELECT pnu, jibun, jimok, area_m2, jiga, "
        "elevation_m, slope_deg, has_road_access, zone_type, prefix8, "
        "shape_type "
        "FROM parcels WHERE " + " AND ".join(where)
        + " ORDER BY area_m2 DESC LIMIT 30"
    )
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(sql, params))
    if not rows:
        # 마지막 fallback: emd/ri 무시하고 jibun만으로 한 번 더
        if target_p8 or where[0].startswith("prefix8"):
            sql2 = (
                "SELECT pnu, jibun, jimok, area_m2, jiga, "
                "elevation_m, slope_deg, has_road_access, zone_type, prefix8 "
                "FROM parcels WHERE (" + " OR ".join("jibun LIKE ?" for _ in patterns)
                + ") ORDER BY area_m2 DESC LIMIT 30"
            )
            rows = list(conn.execute(sql2, patterns))
            if not rows:
                return None
        else:
            return None
    # 같은 jibun에 도로/임야 둘 다 있을 수 있음 → 면적 최대 선택
    # (도로 지목은 보통 좁아 자연스럽게 의도된 본번 임야가 우선됨)
    return dict(rows[0])


# 유사도 레벨 → 기본 허용 폭
SIMILARITY_PRESETS = {
    "strict": {"area_pct": 15, "elev_m": 30, "slope_deg": 3},
    "normal": {"area_pct": 30, "elev_m": 50, "slope_deg": 5},
    "loose":  {"area_pct": 50, "elev_m": 80, "slope_deg": 10},
}


def _resolve_tolerances(cond):
    """우선순위: 항목별 *_tol > similarity_level > 기본(normal). 사용자 명시 min/max_*는
    뒤에서 setdefault로 자동 보존."""
    level = (cond.get("similarity_level") or "normal").lower()
    base = SIMILARITY_PRESETS.get(level, SIMILARITY_PRESETS["normal"])
    return {
        "area_pct": cond.get("area_tol_pct") if cond.get("area_tol_pct") is not None
                    else base["area_pct"],
        "elev_m": cond.get("elevation_tol_m") if cond.get("elevation_tol_m") is not None
                  else base["elev_m"],
        "slope_deg": cond.get("slope_tol_deg") if cond.get("slope_tol_deg") is not None
                     else base["slope_deg"],
        "level": level,
    }


def fill_cond_from_reference(cond, ref_parcel):
    """참조 필지 특성을 cond에 채움. 사용자가 명시한 cond는 안 덮어씀.
    유사도 폭은 similarity_level(기본 normal) + 항목별 *_tol로 결정."""
    notes = []  # 채워진 내역 로그용

    def setdefault(key, value, label):
        if cond.get(key) is None and value is not None:
            cond[key] = value
            notes.append(f"{label}={value}")

    tol = _resolve_tolerances(cond)
    level_label = tol["level"]
    if level_label != "normal" or cond.get("area_tol_pct") is not None \
            or cond.get("elevation_tol_m") is not None \
            or cond.get("slope_tol_deg") is not None:
        notes.append(
            f"tol(level={level_label}, ±{tol['area_pct']}%면적, "
            f"±{tol['elev_m']}m해발, +{tol['slope_deg']}°경사)"
        )

    # jimok
    if not cond.get("jimok_list") and ref_parcel.get("jimok"):
        cond["jimok_list"] = [ref_parcel["jimok"]]
        notes.append(f"jimok={ref_parcel['jimok']}")
    # emd (prefix8 역추적)
    if not cond.get("emd_list") and not cond.get("emd"):
        global _EMD_P8
        if _EMD_P8 is None:
            _EMD_P8 = _load_emd_to_prefix8()
        p8 = ref_parcel.get("prefix8")
        emd_name = next((e for e, p in _EMD_P8.items() if p == p8), None)
        if emd_name:
            cond["emd_list"] = [emd_name]
            notes.append(f"emd={emd_name}")
    # 면적 (tol에 따라 폭 결정)
    area = ref_parcel.get("area_m2")
    if area and area > 0:
        pct = tol["area_pct"] / 100.0
        setdefault("min_area_m2", round(area * (1 - pct)), "min_area_m2")
        setdefault("max_area_m2", round(area * (1 + pct)), "max_area_m2")
    # 해발 (tol에 따라 폭 결정)
    elev = ref_parcel.get("elevation_m")
    if elev is not None:
        setdefault("min_elevation_m", round(elev - tol["elev_m"]), "min_elevation_m")
        setdefault("max_elevation_m", round(elev + tol["elev_m"]), "max_elevation_m")
    # 경사 (참조 + tol)
    slope = ref_parcel.get("slope_deg")
    if slope is not None:
        setdefault("max_slope_deg", round(slope + tol["slope_deg"], 1), "max_slope_deg")
    # 도로 접면 같음
    has_road = ref_parcel.get("has_road_access")
    if has_road == 1 and not cond.get("require_road_access") \
            and not cond.get("exclude_road_access"):
        cond["require_road_access"] = True
        notes.append("require_road_access=True")
    # 용도지역 (있으면)
    zone = ref_parcel.get("zone_type")
    if zone and not cond.get("zone_include") and not cond.get("zone_exclude"):
        cond["zone_include"] = [zone]
        notes.append(f"zone_include=['{zone}']")
    # 형상 (있으면) — 같은 카테고리에 인접 카테고리도 함께 (유사도 normal 이상)
    shape = ref_parcel.get("shape_type")
    if shape and not cond.get("shape_include") and not cond.get("shape_exclude"):
        # 인접 형상 그룹 — strict면 단독, 그 외는 비슷한 모양도 포함
        level = (cond.get("similarity_level") or "normal").lower()
        if level == "strict":
            cond["shape_include"] = [shape]
        else:
            neighbors = {
                "정방형": ["정방형", "장방형"],
                "장방형": ["정방형", "장방형"],
                "길쭉형": ["길쭉형", "장방형"],
                "L자형":  ["L자형", "불규칙"],
                "불규칙": ["불규칙", "L자형"],
            }
            cond["shape_include"] = neighbors.get(shape, [shape])
        notes.append(f"shape_include={cond['shape_include']}")
    return notes


# =====================================================================
#  거리 함수
# =====================================================================
def _seg_dist2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2


def point_to_line_m(p_lon, p_lat, coords):
    if not coords or len(coords) < 2:
        return float("inf")
    lon_m = 111049.0 * cos(radians(p_lat))
    lat_m = 111049.0
    px = p_lon * lon_m
    py = p_lat * lat_m
    min_d2 = float("inf")
    for i in range(len(coords) - 1):
        ax, ay = coords[i][0] * lon_m, coords[i][1] * lat_m
        bx, by = coords[i + 1][0] * lon_m, coords[i + 1][1] * lat_m
        d2 = _seg_dist2(px, py, ax, ay, bx, by)
        if d2 < min_d2:
            min_d2 = d2
    return min_d2 ** 0.5


# =====================================================================
#  SQL 빌드 + 검색
# =====================================================================
def build_period_range(cond, max_ymd):
    """기간 조건 → (start_ymd, end_ymd) 문자열 'YYYY-MM-DD'."""
    # 절대 시간이 우선
    if cond.get("year_start") is not None:
        ys = int(cond["year_start"])
        ye = int(cond.get("year_end") or ys)
        ms = int(cond.get("month_start") or 1)
        me = int(cond.get("month_end") or 12)
        return f"{ys:04d}-{ms:02d}-01", f"{ye:04d}-{me:02d}-31"
    if cond.get("period_months"):
        months = int(cond["period_months"])
        end_dt = datetime.strptime(max_ymd[:10], "%Y-%m-%d") if max_ymd else datetime.now()
        start_dt = end_dt - timedelta(days=int(months * 30.4))
        return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
    return None, None


def search(query: str):
    print("=" * 90)
    print(f" 질의: {query}")
    print("=" * 90)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # [1] 자연어 파싱
    try:
        cond = parse_query(client, query)
    except Exception as e:
        print(f"❌ 파싱 실패: {e}")
        return
    print("\n[1] 자연어 파싱 결과 (null 제외)")
    for k, v in cond.items():
        if v is not None and v != [] and v != "":
            print(f"   {k:22s} = {v!r}")

    # [1.5] 참조 필지 (reference_jibun) — 비슷한 조건 자동 cond 채움
    if cond.get("reference_jibun"):
        ref_str = cond["reference_jibun"]
        ref = lookup_reference_parcel(conn, ref_str)
        if ref:
            py = ref["area_m2"] / 3.3058 if ref.get("area_m2") else 0
            print(f"\n[1.5] 참조 필지 lookup: '{ref_str}'")
            print(f"   PNU={ref['pnu']}  {ref['jibun']}  {ref['jimok']}  "
                  f"{ref['area_m2']:,.0f}㎡({py:,.0f}평)  "
                  f"공시 {ref['jiga']:,.0f}원/㎡  "
                  f"해발 {ref.get('elevation_m')}m  경사 "
                  f"{ref.get('slope_deg'):.1f}°  접면 {ref.get('has_road_access')}"
                  if ref.get("slope_deg") is not None
                  else f"   PNU={ref['pnu']}  {ref['jibun']}  {ref['jimok']}")
            notes = fill_cond_from_reference(cond, ref)
            if notes:
                print(f"   → 자동 채운 cond: {', '.join(notes)}")
        else:
            print(f"\n[1.5] 참조 필지 '{ref_str}' 찾지 못함 — reference 무시")

    # [2] 도로 매핑 — 결과는 항상 리스트로 정규화 (다중 매핑 지원)
    matched_roads = []
    road_lines = None
    if cond.get("road_query"):
        rq = cond["road_query"]
        direct = conn.execute(
            "SELECT 1 FROM roads WHERE road_name = ? LIMIT 1", (rq,)
        ).fetchone()
        if direct:
            matched_roads = [rq]
            print(f"\n[2] 도로: '{rq}' DB 직접 일치")
        else:
            cands = [r[0] for r in conn.execute(
                "SELECT DISTINCT road_name FROM roads "
                "WHERE rd_rank_h IN ('지방도', '국가지원지방도', '일반국도') "
                "AND road_name NOT IN ('', '-') ORDER BY road_name"
            )]
            info = map_road(client, rq, cands)
            m = info.get("matched")
            if isinstance(m, list):
                matched_roads = m
            elif isinstance(m, str) and m:
                matched_roads = [m]
            print(f"\n[2] 도로 매핑: '{rq}' → {matched_roads} ({info.get('confidence')})")
            if info.get("reason"):
                print(f"   이유: {info['reason']}")

    # [3] WHERE 절 빌드
    where = ["resolved_pnu IS NOT NULL"]
    params = []

    # 기간
    max_ymd = conn.execute("SELECT MAX(deal_ymd) FROM trades").fetchone()[0]
    start_ymd, end_ymd = build_period_range(cond, max_ymd)
    if start_ymd:
        where.append("deal_ymd BETWEEN ? AND ?")
        params.extend([start_ymd, end_ymd])
        print(f"\n[3] 기간: {start_ymd} ~ {end_ymd}")

    # emd_list(다중) 우선, 없으면 emd(단수, 옛 호환)
    emds = cond.get("emd_list")
    if not emds and cond.get("emd"):
        emds = [cond["emd"]]
    if emds:
        where.append("(" + " OR ".join("umd_name LIKE ?" for _ in emds) + ")")
        params += [e + "%" for e in emds]

    if cond.get("jimok_list"):
        jms = cond["jimok_list"]
        where.append(f"jimok IN ({','.join('?' * len(jms))})")
        params += jms
    if cond.get("exclude_jimok_list"):
        ex = cond["exclude_jimok_list"]
        where.append(f"jimok NOT IN ({','.join('?' * len(ex))})")
        params += ex

    if cond.get("min_area_m2") is not None:
        where.append("area_m2 >= ?")
        params.append(cond["min_area_m2"])
    if cond.get("max_area_m2") is not None:
        where.append("area_m2 <= ?")
        params.append(cond["max_area_m2"])

    if cond.get("min_deal_amount") is not None:
        where.append("deal_amount >= ?")
        params.append(cond["min_deal_amount"])
    if cond.get("max_deal_amount") is not None:
        where.append("deal_amount <= ?")
        params.append(cond["max_deal_amount"])

    if cond.get("min_unit_per_pyeong") is not None:
        where.append("unit_per_pyeong >= ?")
        params.append(cond["min_unit_per_pyeong"])
    if cond.get("max_unit_per_pyeong") is not None:
        where.append("unit_per_pyeong <= ?")
        params.append(cond["max_unit_per_pyeong"])

    # 입지/규제 조건 (parcels 컬럼) — 서브쿼리로 합침
    parcels_conds = []
    parcels_params = []
    if cond.get("min_elevation_m") is not None:
        parcels_conds.append("elevation_m >= ?")
        parcels_params.append(cond["min_elevation_m"])
    if cond.get("max_elevation_m") is not None:
        parcels_conds.append("elevation_m <= ?")
        parcels_params.append(cond["max_elevation_m"])
    if cond.get("max_slope_deg") is not None:
        parcels_conds.append("slope_deg <= ?")
        parcels_params.append(cond["max_slope_deg"])
    if cond.get("zone_include"):
        zs = cond["zone_include"]
        parcels_conds.append(f"zone_type IN ({','.join('?' * len(zs))})")
        parcels_params += zs
    if cond.get("zone_exclude"):
        zs = cond["zone_exclude"]
        parcels_conds.append(f"zone_type NOT IN ({','.join('?' * len(zs))})")
        parcels_params += zs
    if cond.get("exclude_gb"):
        parcels_conds.append("(is_gb IS NULL OR is_gb = 0)")
    if cond.get("exclude_protected_forest"):
        parcels_conds.append("(is_protected_forest IS NULL OR is_protected_forest = 0)")
    if cond.get("exclude_farm_promote"):
        parcels_conds.append("(is_farm_promote IS NULL OR is_farm_promote = 0)")
    if cond.get("max_stream_dist_m") is not None:
        parcels_conds.append("dist_to_stream_m <= ?")
        parcels_params.append(cond["max_stream_dist_m"])
    if cond.get("min_stream_dist_m") is not None:
        parcels_conds.append("dist_to_stream_m >= ?")
        parcels_params.append(cond["min_stream_dist_m"])
    if cond.get("require_road_access"):
        parcels_conds.append("has_road_access = 1")
    if cond.get("exclude_road_access"):
        parcels_conds.append("(has_road_access IS NULL OR has_road_access = 0)")
    if cond.get("exclude_flood"):
        parcels_conds.append("(flood_risk IS NULL OR flood_risk = 0)")
    if cond.get("shape_include"):
        ss = cond["shape_include"]
        parcels_conds.append(f"shape_type IN ({','.join('?' * len(ss))})")
        parcels_params += ss
    if cond.get("shape_exclude"):
        ss = cond["shape_exclude"]
        parcels_conds.append(f"shape_type NOT IN ({','.join('?' * len(ss))})")
        parcels_params += ss
    if parcels_conds:
        where.append(
            "resolved_pnu IN (SELECT pnu FROM parcels WHERE "
            + " AND ".join(parcels_conds) + ")"
        )
        params += parcels_params

    # 도로 반경 박스 좁힘 (다중 도로: IN 절)
    if matched_roads and cond.get("radius_m"):
        ph = ",".join("?" * len(matched_roads))
        b = conn.execute(
            "SELECT MIN(min_lon), MAX(max_lon), MIN(min_lat), MAX(max_lat) "
            f"FROM roads WHERE road_name IN ({ph})", matched_roads
        ).fetchone()
        if b and b[0] is not None:
            rad_deg = cond["radius_m"] / 111049.0
            where += ["resolved_lon BETWEEN ? AND ?", "resolved_lat BETWEEN ? AND ?"]
            params += [b[0] - rad_deg, b[1] + rad_deg, b[2] - rad_deg, b[3] + rad_deg]
            road_lines = [json.loads(r[0]) for r in conn.execute(
                f"SELECT geometry_json FROM roads WHERE road_name IN ({ph})",
                matched_roads)]

    # 정렬
    sort_by = cond.get("sort_by") or "deal_ymd"
    sort_order = (cond.get("sort_order") or "desc").lower()
    if sort_by not in ("deal_ymd", "unit_per_pyeong", "area_m2", "deal_amount"):
        sort_by = "deal_ymd"
    sort_order = "ASC" if sort_order == "asc" else "DESC"

    sql = (
        "SELECT id, umd_name, jimok, area_m2, deal_amount, deal_ymd, "
        "resolved_pnu, resolved_jibun, resolved_lon, resolved_lat, "
        "unit_per_pyeong, match_confidence "
        "FROM trades WHERE " + " AND ".join(where) +
        f" ORDER BY {sort_by} {sort_order}"
    )
    bbox_results = list(conn.execute(sql, params))
    print(f"\n[4] SQL 1차 필터: {len(bbox_results)}건  (정렬: {sort_by} {sort_order})")

    # 도로 정확 거리
    if road_lines:
        radius_m = cond["radius_m"]
        results = []
        for r in bbox_results:
            d = min(point_to_line_m(r["resolved_lon"], r["resolved_lat"], line)
                    for line in road_lines)
            if d <= radius_m:
                results.append((d, r))
        if sort_by == "deal_ymd":
            results.sort(key=lambda x: x[1]["deal_ymd"], reverse=(sort_order == "DESC"))
        print(f"   도로 반경 {radius_m}m 정확 필터: {len(results)}건")
    else:
        results = [(None, r) for r in bbox_results]

    # 검색 결과 안에서 같은 PNU 묶음 자동 식별 → 라벨 부여
    # (DB의 share_label과 별도 — 검색 결과 내부 빈도 기반)
    pnu_count = Counter(r["resolved_pnu"] for _, r in results)
    def group_label(pnu, n):
        if n <= 1: return "단독"
        if n <= 3: return "공유지분"
        if n <= 7: return "다수공유"
        return "대규모공유"
    # results를 (d, r, group_label) 튜플로 확장
    results = [(d, r, group_label(r["resolved_pnu"], pnu_count[r["resolved_pnu"]]))
               for d, r in results]

    # 공유지분 제외 (요청 시)
    if cond.get("exclude_shared"):
        before = len(results)
        results = [(d, r, g) for d, r, g in results if g == "단독"]
        print(f"   공유지분 제외 후: {len(results)}건  (제외 {before - len(results)})")

    # 결과 출력
    print("\n" + "=" * 90)
    print(f" 결과 {len(results)}건")
    print("=" * 90)
    if not results:
        print("\n조건 만족 거래 없음.")
        conn.close()
        return

    # 시세 요약 — high·단독 기준 (시세 왜곡 방지)
    solo = [(d, r, g) for d, r, g in results
            if r["match_confidence"] == "high" and g == "단독"]
    units = [r["unit_per_pyeong"] for _, r, _ in solo if r["unit_per_pyeong"]]
    if units:
        print(f"\n시세 (high·단독매매 {len(solo)}건 기준):")
        print(f"   평단가 중앙값 {statistics.median(units):>8,.0f} 만원/평")
        print(f"   평단가 평균   {sum(units)/len(units):>8,.0f} 만원/평")
        print(f"   평단가 범위   {min(units):,.0f} ~ {max(units):,.0f}")
        prices = [r["deal_amount"] for _, r, _ in solo]
        print(f"   금액 중앙값   {statistics.median(prices):>8,.0f} 만원")
        # 그룹별 거래 비중
        group_count = Counter(g for _, _, g in results)
        print(f"   그룹: " + "  ".join(f"{k} {v}" for k, v in group_count.most_common()))

    # 미리보기
    print(f"\n[거래 미리보기 — 최대 15건, {sort_by} {sort_order} 정렬]")
    head = "거리   " if road_lines else ""
    print(f"   {head}{'동·리':16s}{'지번':14s}{'지목':4s} "
          f"{'면적':>8s}  {'금액':>10s}  {'평단가':>9s}  시기        신뢰도  그룹")
    print("   " + "-" * 110)
    for d, r, g in results[:15]:
        dstr = f"{d:>4.0f}m " if d is not None else ""
        unit = r["unit_per_pyeong"] or 0
        print(f"   {dstr}{r['umd_name'][:16]:16s}{(r['resolved_jibun'] or '?')[:14]:14s}"
              f"{r['jimok']:4s} {r['area_m2']:>7,.0f}㎡  "
              f"{r['deal_amount']:>9,}만원  {unit:>6,.0f}만원/평  "
              f"{r['deal_ymd'][:10]}  {r['match_confidence']:5s} {g}")

    conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
    else:
        # 테스트 케이스 — 확장 표현 검증
        tests = [
            "원삼면 임야 평당 100 미만 최근 1년",
            "처인구 2024년 상반기 전·답 단독매매만",
            "원삼면 임야 5천만~2억 평단가 낮은 순",
            "용인 덕평로 반경 3km 임야 1억 이하 면적 큰 순",
        ]
        for tq in tests:
            search(tq)
            print("\n\n")
        sys.exit(0)
    search(q)
