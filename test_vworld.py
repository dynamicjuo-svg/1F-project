"""
V-World 연속지적도 LP_PA_CBND_BUBUN — 원삼면 시범 호출 (v2: 진짜 PNU prefix).
원삼면 8자리 prefix는 41461340 (12개 리 공통).
"""

import sys
from collections import Counter
import requests
from api_keys import VWORLD_KEY


URL = "https://api.vworld.kr/req/data"

# 우선 페이지 1만 받아 전체 건수와 응답 형식을 확인
params = {
    "service": "data",
    "request": "GetFeature",
    "data": "LP_PA_CBND_BUBUN",
    "key": VWORLD_KEY,
    "attrFilter": "pnu:like:41461340",   # 원삼면 전체
    "page": "1",
    "size": "1000",
    "geometry": "true",                  # 좌표 포함 (면적 계산용)
    "format": "json",
}

print("=" * 78)
print(" V-World 연속지적도 — 원삼면 전체 (PNU 41461340*), page 1")
print("=" * 78)

resp = requests.get(URL, params=params, timeout=60)
print(f"HTTP {resp.status_code}, {len(resp.text):,} bytes")
data = resp.json()
r = data["response"]
print(f"status: {r.get('status')}")
print(f"record: {r.get('record')}  (total = 원삼면 전체 필지 수)")
print(f"page:   {r.get('page')}")

feats = (r.get("result", {}).get("featureCollection", {}) or {}).get("features", [])
print(f"이 페이지에서 받은 필지: {len(feats)}건")

if not feats:
    sys.exit(0)

# 리(里)별 분포
ri_count = Counter()
for ft in feats:
    pnu = ft["properties"]["pnu"]
    addr = ft["properties"].get("addr", "")
    ri_name = ""
    if "원삼면" in addr:
        parts = addr.split("원삼면", 1)[1].strip().split()
        if parts:
            ri_name = parts[0]
    ri_count[(pnu[:10], ri_name)] += 1

print(f"\n[이 페이지의 리(里)별 필지 수]")
for (prefix, name), cnt in ri_count.most_common():
    print(f"  {prefix}  {name:8s}  {cnt:4d}건")

# 첫 필지 상세 (geometry 길이까지)
first = feats[0]
p = first["properties"]
g = first.get("geometry") or {}
print(f"\n[첫 필지 상세]")
print(f"  PNU      : {p.get('pnu')}")
print(f"  jibun    : {p.get('jibun')}")
print(f"  bonbun   : {p.get('bonbun')}   bubun: {p.get('bubun')}")
print(f"  addr     : {p.get('addr')}")
print(f"  jiga     : {p.get('jiga')}  (공시지가 원/㎡)")
print(f"  geom.type: {g.get('type')}")
coords = g.get("coordinates")
if coords:
    # 다각형 좌표 깊이 추적
    depth = 0
    c = coords
    while isinstance(c, list) and c and isinstance(c[0], list):
        depth += 1
        c = c[0]
    print(f"  geom 좌표 깊이: {depth} (Polygon은 깊이 2, MultiPolygon은 3)")
    print(f"  geom 첫 꼭짓점: {c}")

# 임야(jibun에 '산' 포함) 카운트
imya = [ft for ft in feats if (ft["properties"].get("jibun") or "").startswith("산")]
print(f"\n임야(산) 필지: {len(imya)}건 / 전체 {len(feats)}건")

print("\n" + "=" * 78)
print(" 원삼면 전체 PNU prefix 확정: 41461340 (12개 리)")
print(" → 다음 단계: 좌표에서 m² 면적 계산 + 국토부 거래와 매칭")
print("=" * 78)
