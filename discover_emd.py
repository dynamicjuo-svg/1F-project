"""
시군구 5자리 LAWD_CD에서 읍·면·동별 PNU 8자리 prefix 자동 디스커버리.
V-World 시군구 데이터를 N페이지 표본 추출해 addr에서 읍·면·동 이름을 뽑아 매핑.
결과는 region_prefix_cache.json 에 저장.
"""

import json
import os
import time

import requests
from api_keys import VWORLD_KEY


CACHE_PATH = os.path.join(os.path.dirname(__file__), "region_prefix_cache.json")
URL = "https://api.vworld.kr/req/data"


def discover(sigg_5: str, sample_pages: int = 10) -> dict:
    """sigg_5: 시군구 5자리 LAWD_CD (예: '41461' 용인 처인구)."""
    emd_map = {}
    print(f"V-World에서 시군구 {sigg_5} 데이터 {sample_pages}페이지 받는 중...")
    for page in range(1, sample_pages + 1):
        try:
            r = requests.get(
                URL,
                params={
                    "service": "data", "request": "GetFeature",
                    "data": "LP_PA_CBND_BUBUN", "key": VWORLD_KEY,
                    "attrFilter": f"pnu:like:{sigg_5}",
                    "page": str(page), "size": "1000",
                    "geometry": "false", "format": "json",
                },
                timeout=60,
            )
            data = r.json()
        except Exception as e:
            print(f"  page {page} 오류: {e}")
            continue

        feats = (data.get("response", {}).get("result", {})
                  .get("featureCollection", {}) or {}).get("features", [])
        if not feats:
            break
        for ft in feats:
            addr = ft["properties"].get("addr") or ""
            pnu = ft["properties"]["pnu"]
            parts = addr.split()
            # "경기도 용인시 처인구 양지면 정수리 222-1" → [경기도, 용인시, 처인구, 양지면, ...]
            if len(parts) < 4:
                continue
            sido = parts[0]
            sigg = " ".join(parts[1:3])
            emd = parts[3]
            if emd not in emd_map:
                emd_map[emd] = {
                    "prefix8": pnu[:8],
                    "sample_addr": addr,
                    "sido": sido,
                    "sigg": sigg,
                    "count": 0,
                    "ri_set": set(),
                }
            emd_map[emd]["count"] += 1
            # 리 추출 — 산이나 숫자가 아닌 5번째 토큰만
            if len(parts) >= 5:
                tok = parts[4]
                if tok and not tok[0].isdigit() and tok != "산":
                    emd_map[emd]["ri_set"].add(tok)
        print(f"  page {page}/{sample_pages}: 누적 {len(emd_map)}개 읍·면·동")
        time.sleep(0.15)

    return emd_map


def main():
    sigg_5 = "41461"   # 용인시 처인구
    emd_map = discover(sigg_5, sample_pages=15)

    print(f"\n발견된 읍·면·동 수: {len(emd_map)}")
    print(f"표본 기준 {sigg_5} 시군구: "
          f"{list(emd_map.values())[0]['sido']} {list(emd_map.values())[0]['sigg']}")
    print("-" * 78)
    sorted_emd = sorted(emd_map.items(), key=lambda x: -x[1]["count"])
    for name, info in sorted_emd:
        ri_list = sorted(info["ri_set"])
        ri_s = (f"  리 {len(ri_list)}개 ({', '.join(ri_list[:4])}"
                + ("..." if len(ri_list) > 4 else "") + ")") if ri_list else ""
        print(f"  {info['prefix8']}  {name:10s}  표본 {info['count']:4d}건{ri_s}")

    # 캐시 저장 (set은 list로 변환)
    cache = {
        "sigg_5": sigg_5,
        "sido": sorted_emd[0][1]["sido"] if sorted_emd else "",
        "sigg": sorted_emd[0][1]["sigg"] if sorted_emd else "",
        "emd_map": {
            name: {
                "prefix8": info["prefix8"],
                "sample_addr": info["sample_addr"],
                "count": info["count"],
                "ri_list": sorted(info["ri_set"]),
            }
            for name, info in emd_map.items()
        },
    }
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"\n[캐시 저장] {CACHE_PATH}")


if __name__ == "__main__":
    main()
