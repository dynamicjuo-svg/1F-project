"""
build_db.py — 처인구 5년치 실거래 + V-World 필지를 SQLite (trades.db) 에 적재.

[흐름]
  1. 17개 읍·면·동 필지 적재 (기존 cache_*.json 있으면 그걸 먼저 활용)
  2. 국토부 5년 × 60개월 거래 반복 수집
  3. 거래마다 매칭 엔진 호출 → 복원 지번·좌표·공시지가 한 row에 저장
  4. 이미 처리된 월/필지는 자동 스킵 (다시 실행해도 빠르게)

DB 스키마는 자연어 질의 다음 단계에 그대로 쓸 수 있게 인덱스를 미리 박아둠.
"""

import json
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from math import cos, radians

import requests

from api_keys import MOLIT_KEY, VWORLD_KEY
from jibun_matcher import Trade, Parcel, match_trade


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
REGION_CACHE = os.path.join(HERE, "region_prefix_cache.json")

SIGG_LAWD_CD = "41461"        # 용인 처인구
PERIOD_START = (2021, 1)
PERIOD_END = (2026, 5)


# =====================================================================
#  DB 스키마
# =====================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS parcels (
        pnu TEXT PRIMARY KEY,
        jibun TEXT,
        jimok TEXT,
        area_m2 REAL,
        lon REAL, lat REAL,
        jiga REAL,
        prefix8 TEXT,
        addr TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_parcels_prefix ON parcels(prefix8);
    CREATE INDEX IF NOT EXISTS idx_parcels_jimok ON parcels(jimok);
    CREATE INDEX IF NOT EXISTS idx_parcels_area ON parcels(area_m2);

    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sigg_cd TEXT, umd_name TEXT, jimok TEXT,
        area_m2 REAL,
        jibun_masked TEXT, is_san INTEGER,
        deal_amount INTEGER,
        deal_year TEXT, deal_month TEXT, deal_day TEXT,
        deal_ymd TEXT,
        land_use TEXT, dealing_gbn TEXT,
        match_confidence TEXT,
        resolved_pnu TEXT,
        resolved_jibun TEXT,
        resolved_area_m2 REAL,
        resolved_lon REAL, resolved_lat REAL,
        resolved_jiga REAL,
        unit_per_pyeong REAL,
        candidates_count INTEGER,
        share_label TEXT,
        UNIQUE(sigg_cd, umd_name, jimok, area_m2, jibun_masked,
               deal_year, deal_month, deal_day, deal_amount)
    );
    CREATE INDEX IF NOT EXISTS idx_trades_ym ON trades(deal_year, deal_month);
    CREATE INDEX IF NOT EXISTS idx_trades_jimok ON trades(jimok);
    CREATE INDEX IF NOT EXISTS idx_trades_umd ON trades(umd_name);
    CREATE INDEX IF NOT EXISTS idx_trades_pnu ON trades(resolved_pnu);
    CREATE INDEX IF NOT EXISTS idx_trades_conf ON trades(match_confidence);
    CREATE INDEX IF NOT EXISTS idx_trades_unit ON trades(unit_per_pyeong);
    CREATE INDEX IF NOT EXISTS idx_trades_share ON trades(share_label);

    CREATE TABLE IF NOT EXISTS collect_log (
        sigg_cd TEXT, deal_ymd TEXT,
        n_trades INTEGER,
        ts TEXT,
        PRIMARY KEY (sigg_cd, deal_ymd)
    );
    """)
    conn.commit()
    return conn


# =====================================================================
#  V-World — 필지 받기
# =====================================================================
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
LAT_DEG_TO_M = 111049.0


def _is_lonlat(p):
    x, y = p[0], p[1]
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


def fetch_vworld_for_prefix(prefix8):
    rows = []
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
            page_total = int(resp.get("page", {}).get("total", "1"))
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
            rows.append((
                p["pnu"], jibun, jimok_full, area_m2,
                cx, cy, jiga, prefix8, p.get("addr", "") or "",
            ))
        if page >= page_total:
            break
        page += 1
        time.sleep(0.1)
    return rows


def collect_parcels(conn, prefix_list):
    for emd_name, prefix8 in prefix_list:
        cur = conn.execute("SELECT COUNT(*) FROM parcels WHERE prefix8 = ?", (prefix8,))
        n_existing = cur.fetchone()[0]
        if n_existing > 0:
            print(f"   ✓ {emd_name:8s} ({prefix8})  {n_existing:>6,}필지 이미 적재, 스킵")
            continue

        cache_path = os.path.join(HERE, f"cache_{prefix8}.json")
        if os.path.exists(cache_path):
            print(f"   ← {emd_name:8s} ({prefix8})  JSON 캐시 사용 ... ", end="", flush=True)
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            rows = [
                (d["pnu"], d["jibun"], d["jimok"], d["area_m2"],
                 d["lon"], d["lat"], d.get("jiga", 0), prefix8, "")
                for d in cache["parcels"]
            ]
        else:
            print(f"   → {emd_name:8s} ({prefix8})  V-World 받기 ... ", end="", flush=True)
            t0 = time.time()
            rows = fetch_vworld_for_prefix(prefix8)
            print(f"({time.time()-t0:.1f}초) ", end="")

        conn.executemany(
            "INSERT OR REPLACE INTO parcels "
            "(pnu, jibun, jimok, area_m2, lon, lat, jiga, prefix8, addr) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        print(f"{len(rows):>6,}필지 저장")


# =====================================================================
#  국토부 거래 + 매칭 + 저장
# =====================================================================
def fetch_molit_full(sigg, ymd):
    r = requests.get(
        "https://apis.data.go.kr/1613000/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade",
        params={
            "serviceKey": MOLIT_KEY, "LAWD_CD": sigg,
            "DEAL_YMD": ymd, "numOfRows": "1000", "pageNo": "1",
        },
        timeout=30,
    )
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return None  # API 오류

    out = []
    for it in root.findall(".//item"):
        def g(name):
            v = it.findtext(name)
            return v.strip() if v else ""

        area_s = g("dealArea").replace(",", "")
        price_s = g("dealAmount").replace(",", "")
        jibun = g("jibun")
        is_san = jibun.startswith("산")
        masked = jibun[1:].strip() if is_san else jibun

        out.append({
            "umd": g("umdNm"),
            "jimok": g("jimok"),
            "area": float(area_s) if area_s else 0.0,
            "jibun_masked": jibun,
            "is_san": is_san,
            "bonbeon_masked": masked,
            "deal_amount": int(price_s) if price_s.lstrip("-").isdigit() else 0,
            "deal_year": g("dealYear"),
            "deal_month": g("dealMonth"),
            "deal_day": g("dealDay"),
            "land_use": g("landUse"),
            "dealing_gbn": g("dealingGbn"),
        })
    return out


def load_parcels(conn, sigg_cd):
    cur = conn.execute(
        "SELECT pnu, jibun, jimok, area_m2, lon, lat, jiga "
        "FROM parcels WHERE prefix8 LIKE ?",
        (sigg_cd + "%",),
    )
    parcels, jiga_map = [], {}
    for row in cur:
        parcels.append(Parcel(
            pnu=row["pnu"], jibun=row["jibun"], jimok=row["jimok"],
            area_m2=row["area_m2"], lon=row["lon"], lat=row["lat"],
        ))
        jiga_map[row["pnu"]] = row["jiga"] or 0
    return parcels, jiga_map


def index_by_prefix_jimok(parcels):
    idx = defaultdict(list)
    for p in parcels:
        idx[(p.pnu[:8], p.jimok)].append(p)
    return idx


def umd_to_prefix8(umd_name, emd_to_prefix):
    for emd_name, prefix8 in emd_to_prefix.items():
        if emd_name in umd_name:
            return prefix8
    return None


def collect_month(conn, sigg, ymd, emd_to_prefix, parcel_index, jiga_map):
    cur = conn.execute(
        "SELECT n_trades FROM collect_log WHERE sigg_cd = ? AND deal_ymd = ?",
        (sigg, ymd),
    )
    row = cur.fetchone()
    if row:
        return row["n_trades"], True

    raws = fetch_molit_full(sigg, ymd)
    if raws is None:
        return 0, False

    rows = []
    summary = Counter()
    for r in raws:
        t = Trade(
            lawd_cd="", emd_name=r["umd"], jimok=r["jimok"], area_m2=r["area"],
            bonbeon_masked=r["bonbeon_masked"], deal_price=r["deal_amount"],
            deal_ymd=f"{r['deal_year']}-{r['deal_month'].zfill(2)}",
            is_san=r["is_san"],
        )
        prefix8 = umd_to_prefix8(r["umd"], emd_to_prefix)
        target = parcel_index.get((prefix8, r["jimok"]), []) if prefix8 else []
        result = match_trade(t, target, area_tol=0.02, mid_max=10)
        summary[result.confidence] += 1

        resolved_pnu = result.resolved.pnu if result.resolved else None
        resolved = result.resolved
        resolved_jiga = jiga_map.get(resolved_pnu) if resolved_pnu else None
        unit = None
        if resolved and resolved.area_m2 > 0:
            pyeong = resolved.area_m2 / 3.3058
            unit = r["deal_amount"] / pyeong if pyeong > 0 else None

        deal_ymd_full = f"{r['deal_year']}-{r['deal_month'].zfill(2)}-{r['deal_day'].zfill(2) or '00'}"

        rows.append((
            sigg, r["umd"], r["jimok"], r["area"], r["jibun_masked"], int(r["is_san"]),
            r["deal_amount"],
            r["deal_year"], r["deal_month"], r["deal_day"],
            deal_ymd_full,
            r["land_use"], r["dealing_gbn"],
            result.confidence,
            resolved_pnu,
            resolved.jibun if resolved else None,
            resolved.area_m2 if resolved else None,
            resolved.lon if resolved else None,
            resolved.lat if resolved else None,
            resolved_jiga,
            unit,
            len(result.candidates),
        ))

    conn.executemany(
        "INSERT OR IGNORE INTO trades ("
        "sigg_cd, umd_name, jimok, area_m2, jibun_masked, is_san, deal_amount, "
        "deal_year, deal_month, deal_day, deal_ymd, land_use, dealing_gbn, "
        "match_confidence, resolved_pnu, resolved_jibun, resolved_area_m2, "
        "resolved_lon, resolved_lat, resolved_jiga, unit_per_pyeong, candidates_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO collect_log VALUES (?,?,?,?)",
        (sigg, ymd, len(raws), datetime.now().isoformat()),
    )
    conn.commit()
    return len(raws), False


def yyyymm_range(start, end):
    y, m = start
    while (y, m) <= end:
        yield f"{y:04d}{m:02d}"
        m += 1
        if m > 12:
            m = 1
            y += 1


# =====================================================================
#  메인
# =====================================================================
def main():
    print("=" * 80)
    print(f" 처인구({SIGG_LAWD_CD})  {PERIOD_START[0]}-{PERIOD_START[1]:02d} ~ "
          f"{PERIOD_END[0]}-{PERIOD_END[1]:02d}  실거래 DB 구축")
    print(f" → {DB_PATH}")
    print("=" * 80)

    conn = init_db()

    # 1) V-World 필지 적재
    print(f"\n[1] V-World 처인구 17개 읍·면·동 필지 적재")
    with open(REGION_CACHE, "r", encoding="utf-8") as f:
        region_cache = json.load(f)
    prefix_list = [(name, info["prefix8"]) for name, info in region_cache["emd_map"].items()]
    collect_parcels(conn, prefix_list)

    # 2) 매칭용 인덱스
    print(f"\n[2] 매칭 인덱스 메모리 적재")
    t0 = time.time()
    parcels, jiga_map = load_parcels(conn, SIGG_LAWD_CD)
    parcel_index = index_by_prefix_jimok(parcels)
    emd_to_prefix = {name: info["prefix8"] for name, info in region_cache["emd_map"].items()}
    print(f"   처인구 전체 {len(parcels):,}필지 / {len(parcel_index):,}개 (prefix8×지목) 인덱스  "
          f"({time.time()-t0:.1f}초)")

    # 3) 60개월 거래 수집
    months = list(yyyymm_range(PERIOD_START, PERIOD_END))
    print(f"\n[3] 국토부 토지매매 실거래 {len(months)}개월 수집 + 매칭")
    t0 = time.time()
    total_new = 0
    skipped = 0
    for i, ymd in enumerate(months, 1):
        n, was_skipped = collect_month(
            conn, SIGG_LAWD_CD, ymd, emd_to_prefix, parcel_index, jiga_map
        )
        if was_skipped:
            skipped += 1
        else:
            total_new += n
        if i % 6 == 0 or i == len(months):
            elapsed = time.time() - t0
            print(f"   [{i:2d}/{len(months)}] {ymd}  +{n}건  "
                  f"누적 신규 {total_new:,}건  경과 {elapsed:.0f}초")
        time.sleep(0.05)

    # 4) DB 요약
    print(f"\n[4] DB 요약")
    print("-" * 80)
    n_parcels = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    n_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"   parcels : {n_parcels:>7,}건")
    print(f"   trades  : {n_trades:>7,}건  (이번 신규 {total_new:,}, 이미 있던 월 {skipped})")

    print(f"\n   매칭 분포:")
    cur = conn.execute("SELECT match_confidence, COUNT(*) FROM trades GROUP BY match_confidence")
    conf = dict(cur.fetchall())
    n = sum(conf.values()) or 1
    for k in ("high", "mid", "low"):
        v = conf.get(k, 0)
        print(f"     {k:5s} {v:>6,}건  ({v/n*100:5.1f}%)")

    print(f"\n   연도별 거래량:")
    cur = conn.execute(
        "SELECT deal_year, COUNT(*) FROM trades GROUP BY deal_year ORDER BY deal_year"
    )
    for row in cur:
        print(f"     {row[0]} {row[1]:>6,}건")

    print(f"\n   상위 거래 동·리 (5년 누적):")
    cur = conn.execute(
        "SELECT umd_name, COUNT(*) AS n FROM trades GROUP BY umd_name "
        "ORDER BY n DESC LIMIT 10"
    )
    for row in cur:
        print(f"     {row['umd_name']:18s} {row['n']:>6,}건")

    print(f"\n   지목별:")
    cur = conn.execute(
        "SELECT jimok, COUNT(*) AS n FROM trades GROUP BY jimok ORDER BY n DESC LIMIT 10"
    )
    for row in cur:
        print(f"     {row['jimok']:8s} {row['n']:>6,}건")

    conn.close()
    print("\n" + "=" * 80)
    print(" 완료. trades.db 가 만들어졌어요. SQL로 자유롭게 조회 가능합니다.")
    print("=" * 80)


if __name__ == "__main__":
    main()
