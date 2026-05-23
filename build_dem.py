"""
build_dem.py — SRTM 30m DEM 타일에서 필지별 해발/경사/향 추출.

SRTM .hgt 포맷:
  - 1° × 1° 타일, 3601 × 3601 점 (30m 해상도)
  - big-endian int16, 단위 m
  - 좌상단(NW corner)에서 시작, 행 위→아래, 열 왼→오
  - -32768 또는 -9999는 결측

처인구는 N37E127.hgt 한 타일 안에 다 들어감 (37.0~37.4°N, 127.1~127.5°E).

slope/aspect는 3×3 윈도우 중앙차분으로 계산.
외부 의존성: numpy만.
"""

import gzip
import os
import sqlite3
import time
import urllib.request
import numpy as np
from math import cos, radians, atan, atan2, degrees, sqrt

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")
HGT_FILE = os.path.join(HERE, "N37E127.hgt")
HGT_URL = "https://elevation-tiles-prod.s3.amazonaws.com/skadi/N37/N37E127.hgt.gz"


def download_hgt_if_missing():
    if os.path.exists(HGT_FILE):
        return
    print(f"   {HGT_FILE} 없음 → AWS public S3에서 다운로드...")
    req = urllib.request.Request(HGT_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        gz = resp.read()
    hgt = gzip.decompress(gz)
    with open(HGT_FILE, "wb") as f:
        f.write(hgt)
    print(f"   저장: {HGT_FILE} ({len(hgt):,} bytes)")

TILE_LAT_MIN = 37   # N37E127 → 37°N ~ 38°N
TILE_LON_MIN = 127  # 127°E ~ 128°E
TILE_SIZE = 3601    # 픽셀 한 변 (30m 해상도 → 3601)
CELL_DEG = 1.0 / (TILE_SIZE - 1)  # 셀 한 변(도)
LAT_DEG_TO_M = 111049.0

TARGET_PREFIX8 = ("41461340", "41461350")  # 원삼면·백암면


def load_hgt(path):
    """SRTM .hgt → 2D numpy array (rows=N→S 방향, cols=W→E)."""
    raw = np.fromfile(path, dtype=">i2")  # big-endian int16
    arr = raw.reshape((TILE_SIZE, TILE_SIZE)).astype(np.float32)
    # 결측 처리
    arr[arr <= -32000] = np.nan
    return arr


def lonlat_to_pixel(lon, lat):
    """경위도 → 타일 픽셀 좌표 (row, col).
    타일 좌상단이 (37+1, 127) → row=0이 lat=38, row=3600이 lat=37.
    """
    row = (TILE_LAT_MIN + 1.0 - lat) / CELL_DEG
    col = (lon - TILE_LON_MIN) / CELL_DEG
    return int(round(row)), int(round(col))


def elevation_slope_aspect(dem, row, col, lat):
    """3×3 윈도우 중앙차분으로 elevation/slope/aspect 계산.
    경계/결측은 NaN 반환."""
    if row < 1 or row >= TILE_SIZE - 1 or col < 1 or col >= TILE_SIZE - 1:
        return None, None, None
    z00 = dem[row - 1, col - 1]; z01 = dem[row - 1, col]; z02 = dem[row - 1, col + 1]
    z10 = dem[row, col - 1];     z11 = dem[row, col];     z12 = dem[row, col + 1]
    z20 = dem[row + 1, col - 1]; z21 = dem[row + 1, col]; z22 = dem[row + 1, col + 1]
    block = np.array([z00, z01, z02, z10, z11, z12, z20, z21, z22])
    if np.isnan(block).any():
        return (float(z11) if not np.isnan(z11) else None), None, None
    # 셀 크기 (m)
    cell_x_m = CELL_DEG * LAT_DEG_TO_M * cos(radians(lat))  # 동서 (위도 보정)
    cell_y_m = CELL_DEG * LAT_DEG_TO_M                       # 남북
    # Horn (1981) 가중 중앙차분
    dzdx = ((z02 + 2 * z12 + z22) - (z00 + 2 * z10 + z20)) / (8 * cell_x_m)
    dzdy = ((z20 + 2 * z21 + z22) - (z00 + 2 * z01 + z02)) / (8 * cell_y_m)
    slope_rad = atan(sqrt(dzdx ** 2 + dzdy ** 2))
    slope_deg = degrees(slope_rad)
    # aspect (북=0, 동=90, 남=180, 서=270)
    aspect_rad = atan2(dzdy, -dzdx)
    aspect_deg = (degrees(aspect_rad) + 360) % 360
    return float(z11), slope_deg, aspect_deg


def main():
    download_hgt_if_missing()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 78)
    print(" build_dem — SRTM 30m DEM에서 필지별 해발/경사/향 추출")
    print("=" * 78)

    print("\n[1] SRTM 타일 로드...")
    t0 = time.time()
    dem = load_hgt(HGT_FILE)
    valid = np.sum(~np.isnan(dem))
    print(f"   {TILE_SIZE}×{TILE_SIZE} = {dem.size:,} 픽셀  "
          f"유효 {valid:,} ({valid/dem.size*100:.1f}%)  "
          f"({time.time()-t0:.1f}초)")
    print(f"   유효 elevation 범위: {np.nanmin(dem):.0f}m ~ {np.nanmax(dem):.0f}m")

    print("\n[2] 대상 필지 로드...")
    ph = ",".join("?" * len(TARGET_PREFIX8))
    rows = list(conn.execute(
        f"SELECT pnu, lon, lat FROM parcels "
        f"WHERE prefix8 IN ({ph}) AND lon IS NOT NULL AND lat IS NOT NULL",
        TARGET_PREFIX8))
    print(f"   {len(rows):,}필지")

    print("\n[3] 필지별 해발/경사/향 계산...")
    t0 = time.time()
    updates = []
    n_total = len(rows)
    n_outside = 0
    log_every = max(1, n_total // 10)
    for i, (pnu, lon, lat) in enumerate(rows, 1):
        if not (TILE_LON_MIN <= lon < TILE_LON_MIN + 1
                and TILE_LAT_MIN <= lat < TILE_LAT_MIN + 1):
            n_outside += 1
            updates.append((None, None, None, pnu))
            continue
        r, c = lonlat_to_pixel(lon, lat)
        elev, slope, aspect = elevation_slope_aspect(dem, r, c, lat)
        updates.append((elev, slope, aspect, pnu))
        if i % log_every == 0:
            print(f"   {i:>6,}/{n_total:,}  ({i/n_total*100:>5.1f}%)")
    print(f"   완료 ({time.time()-t0:.1f}초)  타일밖 {n_outside:,}")

    print("\n[4] DB 업데이트...")
    conn.executemany(
        "UPDATE parcels SET elevation_m=?, slope_deg=?, aspect_deg=? WHERE pnu=?",
        updates,
    )
    conn.commit()

    print("\n[5] 결과 통계")
    print("-" * 78)
    for r in conn.execute(
        f"SELECT prefix8, "
        f"  ROUND(AVG(elevation_m), 1), ROUND(MIN(elevation_m), 0), ROUND(MAX(elevation_m), 0), "
        f"  ROUND(AVG(slope_deg), 1), ROUND(MAX(slope_deg), 1) "
        f"FROM parcels WHERE prefix8 IN ({ph}) AND elevation_m IS NOT NULL "
        f"GROUP BY prefix8",
        TARGET_PREFIX8
    ):
        name = '원삼면' if r[0] == '41461340' else ('백암면' if r[0] == '41461350' else r[0])
        print(f"   {name}: 해발 평균 {r[1]}m  ({r[2]}~{r[3]}m)  "
              f"경사 평균 {r[4]}°  최대 {r[5]}°")

    print("\n[6] 지목별 해발/경사 (원삼+백암 합산)")
    print("-" * 78)
    for r in conn.execute(
        f"SELECT jimok, COUNT(*) cnt, "
        f"  ROUND(AVG(elevation_m), 0) avg_elev, "
        f"  ROUND(AVG(slope_deg), 1) avg_slope "
        f"FROM parcels WHERE prefix8 IN ({ph}) AND elevation_m IS NOT NULL "
        f"GROUP BY jimok ORDER BY 2 DESC LIMIT 10",
        TARGET_PREFIX8
    ):
        print(f"   {r[0]:8s}: {r[1]:>6,}건  평균해발 {r[2]}m  평균경사 {r[3]}°")

    conn.close()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
