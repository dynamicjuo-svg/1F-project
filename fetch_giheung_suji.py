"""
fetch_giheung_suji.py — 기흥구(41463)·수지구(41465) 거래 5년치 fetch.

build_db.py의 거래 fetch 로직 재사용. parcels는 V-World 권한 필요로 미적재.
거래만 trades 테이블에 누적 (UNIQUE 키로 중복 방지).

용인시 LAWD_CD:
  41461 처인구 (이미 적재)
  41463 기흥구 (이번)
  41465 수지구 (이번)
"""

import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

from api_keys import MOLIT_KEY
from jibun_matcher import Trade

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

# 용인시 확장 시군구
TARGET_SIGGS = [
    ("41463", "용인 기흥구"),
    ("41465", "용인 수지구"),
]
PERIOD_START = (2021, 1)
PERIOD_END = (2026, 5)


def fetch_molit_month(lawd_cd5, deal_ymd, max_retry=3):
    """국토부 토지매매 실거래가 — 한 달치 fetch."""
    url = ("https://apis.data.go.kr/1613000/RTMSDataSvcLandTrade/"
           "getRTMSDataSvcLandTrade")
    params = {
        "serviceKey": MOLIT_KEY,
        "LAWD_CD": lawd_cd5,
        "DEAL_YMD": deal_ymd,
        "numOfRows": "1000",
        "pageNo": "1",
    }
    for attempt in range(max_retry):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code != 200:
                time.sleep(1.5 * (attempt + 1))
                continue
            root = ET.fromstring(r.text)
            items = root.findall(".//item")
            return items
        except Exception as e:
            if attempt == max_retry - 1:
                print(f"     ❌ {e}")
                return []
            time.sleep(2)
    return []


def parse_item(item, sigg_cd):
    """국토부 응답 한 건 → dict."""
    def get(tag, default=""):
        el = item.find(tag)
        return el.text.strip() if (el is not None and el.text) else default

    jibun = get("jibun")
    is_san = jibun.startswith("산")
    masked = jibun[1:].strip() if is_san else jibun

    area_m2 = float(get("dealArea", "0").replace(",", "") or "0")
    deal_amount = int(get("dealAmount", "0").replace(",", "").strip() or "0")
    y = get("dealYear", "")
    m = get("dealMonth", "0").zfill(2)
    d = get("dealDay", "0").zfill(2)
    deal_ymd = f"{y}-{m}-{d}" if y else ""

    return {
        "sigg_cd": sigg_cd,
        "umd_name": get("umdNm"),
        "jimok": get("jimok"),
        "area_m2": area_m2,
        "jibun_masked": masked,
        "is_san": 1 if is_san else 0,
        "deal_amount": deal_amount,
        "deal_year": y,
        "deal_month": m,
        "deal_day": d,
        "deal_ymd": deal_ymd,
        "land_use": get("landUse"),
        "dealing_gbn": get("dealingGbn"),
    }


def insert_trades(conn, trades):
    """trades 테이블에 INSERT OR IGNORE (중복 방지)."""
    n_new = 0
    for t in trades:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO trades "
                "(sigg_cd, umd_name, jimok, area_m2, jibun_masked, is_san, "
                " deal_amount, deal_year, deal_month, deal_day, deal_ymd, "
                " land_use, dealing_gbn) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t["sigg_cd"], t["umd_name"], t["jimok"], t["area_m2"],
                    t["jibun_masked"], t["is_san"], t["deal_amount"],
                    t["deal_year"], t["deal_month"], t["deal_day"],
                    t["deal_ymd"], t["land_use"], t["dealing_gbn"],
                )
            )
            if conn.total_changes:
                n_new += 1
        except Exception:
            pass
    return n_new


def main():
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" fetch_giheung_suji — 용인 기흥·수지 거래 5년치 fetch")
    print("=" * 78)

    months = []
    y, m = PERIOD_START
    while (y, m) <= PERIOD_END:
        months.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1

    total_new = 0
    for sigg_cd, sigg_name in TARGET_SIGGS:
        print(f"\n[{sigg_name} {sigg_cd}] {len(months)} 개월")
        sigg_new = 0
        for i, ymd in enumerate(months, 1):
            items = fetch_molit_month(sigg_cd, ymd)
            if items:
                trades = [parse_item(it, sigg_cd) for it in items]
                n_new = insert_trades(conn, trades)
                conn.commit()
                sigg_new += n_new
                print(f"  {ymd}: {len(items):>4} items  +{n_new:>4} new"
                      f"  ({i}/{len(months)})")
            else:
                print(f"  {ymd}: 0 items  ({i}/{len(months)})")
            time.sleep(0.3)  # rate limit
        print(f"  → {sigg_name} 신규 {sigg_new:,}건")
        total_new += sigg_new

    print(f"\n총 신규 거래: {total_new:,}건")

    # 최종 통계
    print("\n[최종 통계] trades 테이블")
    print("-" * 78)
    for r in conn.execute(
        "SELECT sigg_cd, COUNT(*) FROM trades GROUP BY sigg_cd ORDER BY 1"
    ):
        sigg_name = {
            "41461": "용인 처인구", "41463": "용인 기흥구",
            "41465": "용인 수지구",
        }.get(r[0], r[0])
        print(f"  {r[0]} {sigg_name:12s}: {r[1]:>7,}건")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
