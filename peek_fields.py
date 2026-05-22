"""
국토부 응답의 진짜 필드 이름을 한 번 들여다보기 위한 임시 스크립트.
첫 번째 거래 1건의 모든 필드를 그대로 출력합니다.
(필드 이름 확인하고 나면 이 파일은 지워도 됩니다.)
"""

import xml.etree.ElementTree as ET
import requests
from api_keys import MOLIT_KEY

resp = requests.get(
    "https://apis.data.go.kr/1613000/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade",
    params={
        "serviceKey": MOLIT_KEY,
        "LAWD_CD": "41461",
        "DEAL_YMD": "202511",
        "numOfRows": "5",
        "pageNo": "1",
    },
    timeout=20,
)

root = ET.fromstring(resp.text)
items = root.findall(".//item")
print(f"받은 거래: {len(items)}건\n")

for idx, item in enumerate(items[:2], 1):
    print(f"━━━ 거래 #{idx} 의 모든 필드 ━━━")
    for child in item:
        val = (child.text or "").strip()
        print(f"  {child.tag:25s} = {val!r}")
    print()
