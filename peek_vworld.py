"""
V-World 임시 디버깅 v4 — 처인구 1000건을 받아 addr에 '원삼면'이 들어가는 필지를
찾고, 그 PNU의 앞 10자리(법정동코드) prefix를 알아냅니다.
이 prefix가 잡히면 다음 호출부터는 그것을 attrFilter로 써서 원삼면만 받을 수 있어요.
"""

import requests
from collections import Counter
from api_keys import VWORLD_KEY


def fetch_page(page):
    r = requests.get(
        "https://api.vworld.kr/req/data",
        params={
            "service": "data",
            "request": "GetFeature",
            "data": "LP_PA_CBND_BUBUN",
            "key": VWORLD_KEY,
            "attrFilter": "pnu:like:41461",
            "page": str(page),
            "size": "1000",
            "geometry": "false",
            "format": "json",
        },
        timeout=60,
    )
    data = r.json()
    return data["response"]["result"]["featureCollection"]["features"]


wonsam_prefixes = Counter()
prefix_sample_addr = {}
total_seen = 0

# 최대 page 5까지 훑어봄 (5000건 = 처인구 266,974 중 일부)
for pg in range(1, 6):
    feats = fetch_page(pg)
    total_seen += len(feats)
    print(f"page {pg}: {len(feats)}건 받음 (누적 {total_seen}건)")
    for ft in feats:
        addr = ft["properties"].get("addr") or ""
        if "원삼면" in addr:
            prefix = ft["properties"]["pnu"][:10]
            wonsam_prefixes[prefix] += 1
            if prefix not in prefix_sample_addr:
                prefix_sample_addr[prefix] = addr
    if wonsam_prefixes:
        # 첫 발견되면 일찍 멈추는 옵션도 있지만, 여러 리에 걸쳐있을 수 있어 5페이지까지는 봄
        pass

print(f"\n탐색한 처인구 필지: {total_seen}건")
print(f"그 중 원삼면 필지: {sum(wonsam_prefixes.values())}건")
print(f"\n원삼면 PNU 10자리(법정동코드) prefix 분포 — 각 리(里)별:")
print("-" * 78)
for prefix, cnt in wonsam_prefixes.most_common():
    print(f"  {prefix}  {cnt:4d}건  예시: {prefix_sample_addr[prefix]}")

if wonsam_prefixes:
    # PNU 앞 8자리(읍면동까지) 그룹화 — 원삼면 자체 코드
    emd_prefix = Counter()
    for prefix, cnt in wonsam_prefixes.items():
        emd_prefix[prefix[:8]] += cnt
    print(f"\n원삼면 PNU 앞 8자리 prefix (읍면동까지):")
    for prefix, cnt in emd_prefix.most_common():
        print(f"  {prefix}  {cnt}건")
