"""
test_anthropic.py — Anthropic API 키 동작 + 자연어 파싱 첫 시도.
J의 비전 질의 1줄을 JSON 조건 구조로 분해해 보기.
"""

import json
import anthropic

from api_keys import ANTHROPIC_KEY


PARSER_SYSTEM = """당신은 한국 토지 실거래가 검색 시스템의 자연어 질의 파서입니다.
사용자 한 줄 질의를 JSON 객체로만 출력하세요.

스키마(모든 키 포함, 없는 값은 null):
{
  "sigg": "시·군·구 (예: '용인시 처인구')",
  "emd": "읍·면·동 (예: '원삼면')",
  "road_query": "도로 식별자. 도로명(예: '덕평로') 또는 도로번호(예: '지방도318')",
  "radius_m": "도로 반경 m(정수)",
  "jimok_list": "지목 배열 (예: ['임야'] 또는 ['전','답']). 가능 값: 전/답/과수원/목장용지/임야/대/공장용지/창고용지/주차장/주유소용지/도로/철도용지/제방/하천/구거/유지/양어장/수도용지/공원/체육용지/유원지/종교용지/사적지/묘지/잡종지",
  "period_months": "기간 개월수(정수). '1년간'→12, '6개월'→6, '최근 3년'→36, '5년치'→60",
  "min_area_m2": "최소 면적 m²(숫자). '1000평'→3306",
  "max_area_m2": "최대 면적 m²(숫자)"
}

규칙:
- "반경 5km"→5000, "반경 2km"→2000
- "1년간"/"작년"→12개월. 1평≈3.3058㎡
- 시군구가 안 명시되어도 읍면동 이름으로 짐작 가능하면 채울 것 (예: '원삼면'→'용인시 처인구')
- 추정 못 하는 필드는 null"""


def parse(client, query: str):
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=PARSER_SYSTEM,
        messages=[
            {"role": "user", "content": query},
            {"role": "assistant", "content": "{"},   # prefill — JSON 강제
        ],
    )
    # prefill 한 '{' 다시 붙여줘야 완전한 JSON
    return "{" + msg.content[0].text


def main():
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    queries = [
        "용인시 원삼면에 지방도318 반경 5km 이내 임야의 1년간 실거래가",
        "원삼면 임야 1000평 이상 최근 3년",
        "처인구 덕평로 반경 2km 안 전·답 작년 거래",
    ]

    for q in queries:
        print("=" * 80)
        print(f"질의: {q}")
        print("-" * 80)
        try:
            resp = parse(client, q)
        except anthropic.AuthenticationError as e:
            print(f"❌ 인증 실패 (키 문제): {e}")
            return
        except anthropic.PermissionDeniedError as e:
            print(f"❌ 권한 거부 (대개 결제 미등록): {e}")
            return
        except anthropic.APIError as e:
            print(f"❌ API 오류: {e}")
            return
        except Exception as e:
            print(f"❌ 예상치 못한 오류: {type(e).__name__}: {e}")
            return

        print("LLM 원문 응답:")
        print(resp)
        print()
        try:
            parsed = json.loads(resp)
            print("✓ JSON 파싱 성공:")
            for k, v in parsed.items():
                print(f"   {k:18s} = {v!r}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON 파싱 실패: {e}")
        print()


if __name__ == "__main__":
    main()
