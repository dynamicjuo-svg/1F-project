"""
국토부 토지 매매 실거래가 — 시범 호출 (v2: 실제 필드 이름 반영).
용인시 처인구(LAWD_CD=41461), 2025년 11월.

실제 응답 필드:
  umdNm        법정동(읍/면/동/리)
  jimok        지목 (대/전/답/임야/...)
  dealArea     거래면적(㎡)
  jibun        지번 (별표 포함; 임야인 경우 '산 5**' 형태 가능)
  dealAmount   거래금액(만원)
  dealYear/Month/Day  계약일
  sggCd        시군구코드, sggNm 시군구명
  landUse      용도지역
"""

import sys
import xml.etree.ElementTree as ET
from collections import Counter

import requests
from api_keys import MOLIT_KEY


URL = "https://apis.data.go.kr/1613000/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade"

params = {
    "serviceKey": MOLIT_KEY,
    "LAWD_CD": "41461",      # 용인시 처인구
    "DEAL_YMD": "202511",    # 2025년 11월
    "numOfRows": "1000",
    "pageNo": "1",
}

print("=" * 78)
print(" 국토부 토지 실거래가 — 용인시 처인구, 2025-11")
print("=" * 78)

resp = requests.get(URL, params=params, timeout=20)
root = ET.fromstring(resp.text)
print(f"API 응답: {root.findtext('.//resultCode')} "
      f"({root.findtext('.//resultMsg')})")

items = root.findall(".//item")
print(f"받은 거래: {len(items)}건")


def f(item, name):
    v = item.findtext(name)
    return v.strip() if v else ""


# 1) 지목별 분포
print("\n[지목별 거래 수]")
for k, v in Counter(f(it, "jimok") for it in items).most_common():
    print(f"  {k:6s} {v}건")

# 2) 임야 거래 따로 들여다보기 (지번 표기 패턴 확인 — '산 5**' 인지)
imya = [it for it in items if f(it, "jimok") == "임야"]
print(f"\n[임야 거래: {len(imya)}건 — 처음 8건의 지번 표기]")
print("-" * 78)
for i, item in enumerate(imya[:8], 1):
    print(f"{i:2d}. {f(item,'umdNm'):14s}  "
          f"지번={f(item,'jibun')!r:14s}  "
          f"{f(item,'dealArea'):>8s}㎡  "
          f"{f(item,'dealAmount'):>10s}만원  "
          f"({f(item,'dealYear')}-{f(item,'dealMonth')}-{f(item,'dealDay')})")

# 3) 원삼면 거래만
won = [it for it in items if "원삼면" in f(it, "umdNm")]
print(f"\n[원삼면 거래: {len(won)}건 — 처음 8건]")
print("-" * 78)
for i, item in enumerate(won[:8], 1):
    print(f"{i:2d}. {f(item,'umdNm'):14s}  "
          f"{f(item,'jimok'):4s}  "
          f"지번={f(item,'jibun')!r:14s}  "
          f"{f(item,'dealArea'):>8s}㎡  "
          f"{f(item,'dealAmount'):>10s}만원")

print("\n" + "=" * 78)
print(" 관찰 포인트")
print("=" * 78)
print(" - 'jibun' 필드는 본번+부번을 합친 별표 표기 (예: '3**', '*', '산 5**')")
print(" - 산구분은 jibun 앞에 '산 ' 접두어가 붙는지로 판별")
print(" - 매칭 엔진(jibun_matcher.py)으로 이 데이터를 보내려면 jibun 문자열을")
print("   `is_san` + `bonbeon_masked` 로 분해하는 작은 변환만 추가하면 됩니다.")
