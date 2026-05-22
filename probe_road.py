"""
probe_road v2 — 원삼면 박스 안 도로 데이터 분석.
"지방도318" 표기 형식과 라인 데이터 구조 확인.
"""

import sqlite3
import time
from collections import Counter

import requests
from api_keys import VWORLD_KEY


# 원삼면 박스 (DB에서 추출)
conn = sqlite3.connect("trades.db")
row = conn.execute(
    "SELECT MIN(lon), MAX(lon), MIN(lat), MAX(lat) "
    "FROM parcels WHERE prefix8 = '41461340'"
).fetchone()
conn.close()
min_lon, max_lon, min_lat, max_lat = row
print(f"원삼면 박스: lon {min_lon:.4f}~{max_lon:.4f}, lat {min_lat:.4f}~{max_lat:.4f}")

# 5km 마진 확장 (5km ≈ 0.045도, 안전하게 0.05)
margin = 0.05
bbox = f"{min_lon-margin:.4f},{min_lat-margin:.4f},{max_lon+margin:.4f},{max_lat+margin:.4f}"
print(f"확장 박스(±5km): {bbox}\n")


all_links = []
page = 1
while True:
    r = requests.get(
        "https://api.vworld.kr/req/data",
        params={
            "service": "data", "request": "GetFeature",
            "data": "LT_L_MOCTLINK", "key": VWORLD_KEY,
            "geomFilter": f"BOX({bbox})",
            "page": str(page), "size": "1000",
            "geometry": "false", "format": "json",
        },
        timeout=60,
    )
    data = r.json()
    resp = data.get("response", {})
    if resp.get("status") != "OK":
        print(f"page {page}: status={resp.get('status')}, 종료")
        break
    feats = (resp.get("result", {}).get("featureCollection", {}) or {}).get("features", [])
    if not feats:
        break
    all_links.extend(feats)
    page_total = int(resp.get("page", {}).get("total", "1"))
    print(f"  page {page}/{page_total}: {len(feats)}건  누적 {len(all_links):,}")
    if page >= page_total:
        break
    page += 1
    time.sleep(0.1)

print(f"\n총 {len(all_links):,}개 도로 링크")

# 도로 위계
print(f"\n[도로 위계(rd_rank_h) 분포]")
ranks = Counter(ft["properties"].get("rd_rank_h", "") for ft in all_links)
for rank, cnt in ranks.most_common():
    print(f"  {cnt:>6,}  {rank!r}")

# 고유 도로명 수
names = Counter(ft["properties"].get("road_name", "") for ft in all_links)
print(f"\n[고유 도로명 수: {len(names):,}]")
print(f"가장 많이 등장 상위 15개:")
for name, cnt in names.most_common(15):
    print(f"  {cnt:>5,}  {name!r}")

# "지방도" 카테고리 추출
print(f"\n[rd_rank_h == '지방도' 도로명들]")
local_road_links = [
    ft for ft in all_links
    if ft["properties"].get("rd_rank_h") == "지방도"
]
local_names = Counter(ft["properties"].get("road_name", "") for ft in local_road_links)
for name, cnt in local_names.most_common():
    print(f"  {cnt:>5,}  {name!r}")

# "국도" 카테고리도 같이
print(f"\n[rd_rank_h == '국도' 도로명들]")
nat_links = [
    ft for ft in all_links
    if ft["properties"].get("rd_rank_h") == "국도"
]
nat_names = Counter(ft["properties"].get("road_name", "") for ft in nat_links)
for name, cnt in nat_names.most_common():
    print(f"  {cnt:>5,}  {name!r}")

# 검색: 318 또는 지방도 포함
print(f"\n[키워드 '318' 또는 '지방도' 포함]")
for name, cnt in names.most_common():
    if "318" in name or "지방도" in name:
        print(f"  {cnt:>5,}  {name!r}")
