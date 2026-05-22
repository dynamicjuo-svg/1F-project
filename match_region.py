"""
match_region.py — 임의 읍·면·동 매칭 (일반화 검증용).

region_prefix_cache.json 에서 prefix를 자동 조회해 매칭을 돌리고,
**시세 분석 관점**으로 결과를 출력 (단독매매 기반 평단가 중앙값 등).

사용: 하단 EMD_NAME 만 바꾸고 실행.
※ match_wonsam.py와 핵심 함수 중복. 검증 끝나면 통합 예정.
"""

import json
import os
import statistics
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from math import cos, radians

import requests

from api_keys import MOLIT_KEY, VWORLD_KEY
from jibun_matcher import Trade, Parcel, match_trade


# =============================
#  설정 — 검증할 지역
# =============================
EMD_NAME = "백암면"            # 읍·면·동 이름 (region_prefix_cache.json 의 키)
SIGG_LAWD_CD = "41461"         # 시군구 5자리 (용인 처인구)
DEAL_YMD = "202511"            # 거래월 YYYYMM
REGION_CACHE_FILENAME = "region_prefix_cache.json"


# =============================
#  핵심 함수 (match_wonsam.py 와 동일)
# =============================
LAT_DEG_TO_M = 111049.0

JIMOK_MAP = {
    "전": "전", "답": "답", "과": "과수원", "목": "목장용지",
    "임": "임야", "광": "광천지", "염": "염전", "대": "대",
    "장": "공장용지", "학": "학교용지", "차": "주차장",
    "주": "주유소용지", "창": "창고용지", "도": "도로",
    "철": "철도용지", "제": "제방", "천": "하천", "구": "구거",
    "유": "유지", "양": "양어장", "수": "수도용지",
    "공": "공원", "체": "체육용지", "원": "유원지",
    "종": "종교용지", "사": "사적지", "묘": "묘지", "잡": "잡종지",
}


def fetch_molit_trades(lawd_cd5, deal_ymd):
    r = requests.get(
        "https://apis.data.go.kr/1613000/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade",
        params={"serviceKey": MOLIT_KEY, "LAWD_CD": lawd_cd5,
                "DEAL_YMD": deal_ymd, "numOfRows": "1000", "pageNo": "1"},
        timeout=30,
    )
    root = ET.fromstring(r.text)
    out = []
    for it in root.findall(".//item"):
        umd = (it.findtext("umdNm") or "").strip()
        jimok = (it.findtext("jimok") or "").strip()
        area_s = (it.findtext("dealArea") or "0").strip().replace(",", "")
        area = float(area_s) if area_s else 0.0
        jibun = (it.findtext("jibun") or "").strip()
        price_s = (it.findtext("dealAmount") or "0").strip().replace(",", "")
        price = int(price_s) if price_s.lstrip("-").isdigit() else 0
        y = (it.findtext("dealYear") or "").strip()
        m = (it.findtext("dealMonth") or "").strip()
        is_san = jibun.startswith("산")
        masked = jibun[1:].strip() if is_san else jibun
        out.append(Trade(
            lawd_cd="", emd_name=umd, jimok=jimok, area_m2=area,
            bonbeon_masked=masked, deal_price=price,
            deal_ymd=f"{y}-{m.zfill(2)}", is_san=is_san,
        ))
    return out


def _is_lonlat(point):
    x, y = point[0], point[1]
    return 100 < x < 160 and 20 < y < 50


def ring_area_m2(ring):
    if not ring:
        return 0.0
    if _is_lonlat(ring[0]):
        avg_lat = sum(p[1] for p in ring) / len(ring)
        lon_m = LAT_DEG_TO_M * cos(radians(avg_lat))
        lat_m = LAT_DEG_TO_M
        s = 0.0
        n = len(ring)
        for i in range(n):
            x1 = ring[i][0] * lon_m
            y1 = ring[i][1] * lat_m
            x2 = ring[(i + 1) % n][0] * lon_m
            y2 = ring[(i + 1) % n][1] * lat_m
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_total(coords, geom_type):
    polys = coords if geom_type == "MultiPolygon" else [coords]
    total = 0.0
    first_outer = None
    for poly in polys:
        outer = poly[0]
        if first_outer is None:
            first_outer = outer
        total += ring_area_m2(outer)
        for hole in poly[1:]:
            total -= ring_area_m2(hole)
    cx = sum(p[0] for p in first_outer) / len(first_outer)
    cy = sum(p[1] for p in first_outer) / len(first_outer)
    return total, cx, cy


def extract_jimok_char(jibun, bubun):
    for s in (jibun, bubun):
        s = (s or "").strip()
        if s and not s[-1].isdigit():
            return s[-1]
    return ""


def fetch_vworld_parcels(prefix8):
    parcels, jiga_map = [], {}
    page = 1
    page_total = None
    while True:
        r = requests.get(
            "https://api.vworld.kr/req/data",
            params={
                "service": "data", "request": "GetFeature",
                "data": "LP_PA_CBND_BUBUN", "key": VWORLD_KEY,
                "attrFilter": f"pnu:like:{prefix8}",
                "page": str(page), "size": "1000",
                "geometry": "true", "format": "json",
            },
            timeout=60,
        )
        data = r.json()
        resp = data.get("response", {})
        if page == 1:
            total_rec = resp.get("record", {}).get("total", "?")
            page_total = int(resp.get("page", {}).get("total", "1"))
            print(f"   전체 {total_rec}필지, {page_total}페이지")
        feats = (resp.get("result", {}).get("featureCollection", {}) or {}).get("features", [])
        if not feats:
            break
        for ft in feats:
            p = ft["properties"]
            g = ft.get("geometry") or {}
            try:
                area_m2, cx, cy = polygon_total(g["coordinates"], g["type"])
            except Exception:
                area_m2, cx, cy = 0.0, 0.0, 0.0
            jibun = (p.get("jibun") or "").strip()
            bubun = (p.get("bubun") or "").strip()
            jimok_full = JIMOK_MAP.get(extract_jimok_char(jibun, bubun), "?")
            try:
                jiga = float(p.get("jiga") or 0)
            except (TypeError, ValueError):
                jiga = 0.0
            parcels.append(Parcel(
                pnu=p["pnu"], jibun=jibun, jimok=jimok_full,
                area_m2=area_m2, lon=cx, lat=cy,
            ))
            jiga_map[p["pnu"]] = jiga
        print(f"   page {page}/{page_total}  누적 {len(parcels):,}필지")
        if page >= page_total:
            break
        page += 1
        time.sleep(0.15)
    return parcels, jiga_map


def load_or_fetch_parcels(prefix8, cache_path):
    if os.path.exists(cache_path):
        print(f"   [캐시 사용] {os.path.basename(cache_path)}")
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        parcels = [Parcel(
            pnu=d["pnu"], jibun=d["jibun"], jimok=d["jimok"],
            area_m2=d["area_m2"], lon=d["lon"], lat=d["lat"],
        ) for d in cache["parcels"]]
        jiga_map = {d["pnu"]: d.get("jiga", 0) for d in cache["parcels"]}
        return parcels, jiga_map

    print("   [캐시 없음] V-World에서 받아옵니다 (시간 좀 걸려요)")
    parcels, jiga_map = fetch_vworld_parcels(prefix8)
    serial = [{
        "pnu": p.pnu, "jibun": p.jibun, "jimok": p.jimok,
        "area_m2": p.area_m2, "lon": p.lon, "lat": p.lat,
        "jiga": jiga_map.get(p.pnu, 0),
    } for p in parcels]
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"prefix8": prefix8, "parcels": serial}, f, ensure_ascii=False)
    print(f"   [캐시 저장] {cache_path}")
    return parcels, jiga_map


# =============================
#  메인 — 시세 검증 출력
# =============================
def main():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, REGION_CACHE_FILENAME), "r", encoding="utf-8") as f:
        region_cache = json.load(f)

    if EMD_NAME not in region_cache["emd_map"]:
        print(f"❌ '{EMD_NAME}' 가 region_prefix_cache.json 에 없습니다.")
        print(f"가능한 이름: {', '.join(region_cache['emd_map'].keys())}")
        return

    info = region_cache["emd_map"][EMD_NAME]
    prefix8 = info["prefix8"]
    parcel_cache = os.path.join(here, f"cache_{prefix8}.json")

    print("=" * 80)
    print(f" 시세 검증 — {region_cache.get('sido','')} "
          f"{region_cache.get('sigg','')} {EMD_NAME}  ({DEAL_YMD})")
    print(f" PNU prefix: {prefix8}    필지 캐시: cache_{prefix8}.json")
    print("=" * 80)

    # [1] 거래 받기
    print("\n[1] 국토부 거래 받는 중...")
    trades_all = fetch_molit_trades(SIGG_LAWD_CD, DEAL_YMD)
    trades = [t for t in trades_all if EMD_NAME in t.emd_name]
    print(f"   시군구 전체 {len(trades_all)}건  →  {EMD_NAME} {len(trades)}건")
    if not trades:
        print(f"❌ {EMD_NAME} 거래 없음. DEAL_YMD 를 다른 달로 시도해보세요.")
        return

    # [2] 필지 받기
    print(f"\n[2] V-World {EMD_NAME} 필지 적재")
    t0 = time.time()
    parcels, jiga_map = load_or_fetch_parcels(prefix8, parcel_cache)
    print(f"   {len(parcels):,}필지  ({time.time() - t0:.1f}초)")

    # [3] 매칭
    print(f"\n[3] 매칭 (mid_max=10, area_tol=2%)")
    results = [match_trade(t, parcels, area_tol=0.02, mid_max=10) for t in trades]
    summary = Counter(r.confidence for r in results)
    N = len(results)
    high_pct = summary["high"] / N * 100 if N else 0
    partial_pct = (summary["high"] + summary["mid"]) / N * 100 if N else 0
    print(f"   확정 {summary['high']:3d}  후보다수 {summary['mid']:3d}  "
          f"실패 {summary['low']:3d}  /  총 {N}건")
    print(f"   ▶ 자동 복원율 {high_pct:.1f}%   부분 복원율 {partial_pct:.1f}%")

    # [4] 단독매매 / 공유지분 분리 (시세 노이즈 필터링)
    pnu_groups = defaultdict(list)
    for r in results:
        if r.confidence == "high" and r.resolved:
            pnu_groups[r.resolved.pnu].append(r)

    solo_trades, shared_count = [], 0
    for pnu, lst in pnu_groups.items():
        if len(lst) == 1:
            solo_trades.append(lst[0])
        else:
            shared_count += len(lst)

    print(f"\n[4] 시세 노이즈 분리 (확정 {summary['high']}건 기준)")
    print("-" * 80)
    print(f"   · 단독매매(정상 시세):  {len(solo_trades)}건")
    print(f"   · 공유지분 거래(시세 평균에서 제외): {shared_count}건")

    # [5] 지목별 평단가 (단독매매만)
    print(f"\n[5] 지목별 정상 거래 평단가 — {EMD_NAME}")
    print("-" * 80)
    jimok_units = defaultdict(list)
    for r in solo_trades:
        pyeong = r.resolved.area_m2 / 3.3058
        if pyeong > 0:
            jimok_units[r.trade.jimok].append(r.trade.deal_price / pyeong)
    if jimok_units:
        for jimok in sorted(jimok_units.keys(), key=lambda k: -len(jimok_units[k])):
            u = jimok_units[jimok]
            print(f"   {jimok:6s}  {len(u):3d}건  "
                  f"중앙값 {statistics.median(u):>6,.0f}만원/평  "
                  f"평균 {sum(u)/len(u):>6,.0f}만원/평  "
                  f"범위 {min(u):>5,.0f}~{max(u):,.0f}")
    else:
        print("   (단독매매가 없어 시세 산출 불가)")

    # [6] 정상 거래 미리보기
    print(f"\n[6] 정상 거래 미리보기 (단독매매, 최대 10건)")
    print("-" * 80)
    for i, r in enumerate(solo_trades[:10], 1):
        t, p = r.trade, r.resolved
        pyeong = p.area_m2 / 3.3058
        unit = (t.deal_price / pyeong) if pyeong else 0
        print(f"   {i:2d}. {t.emd_name:14s} {p.jibun:14s}  "
              f"{t.jimok:4s} {t.area_m2:>7,.0f}㎡  "
              f"{t.deal_price:>10,}만원  {unit:>5,.0f}만원/평")

    # [7] 원삼면과 비교
    print("\n" + "=" * 80)
    print(" 원삼면과 비교 (둘 다 2025-11):")
    print(f"   원삼면 :  거래 135 / 확정 50 (37.0%) / 부분 78%")
    print(f"   {EMD_NAME} :  거래 {N} / 확정 {summary['high']} "
          f"({high_pct:.1f}%) / 부분 {partial_pct:.1f}%")
    print("=" * 80)


if __name__ == "__main__":
    main()
