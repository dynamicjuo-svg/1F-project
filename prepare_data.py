"""
prepare_data.py — Railway 시작 전 trades.db 준비.

로컬:
  trades.db가 이미 있으면 그대로 통과.
Railway:
  trades.db 없으면 환경변수 TRADES_DB_URL (또는 기본값)에서 다운로드.
  GitHub Releases에 미리 업로드된 데이터를 받음 → V-World API 호출 0번.
"""

import os
import sys
import time
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "trades.db")

DEFAULT_DB_URL = (
    "https://github.com/dynamicjuo-svg/1F-project/releases/download/"
    "v1.0-data/trades.db"
)
DB_URL = os.environ.get("TRADES_DB_URL", DEFAULT_DB_URL)


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


_last_print = 0.0


def _progress(block, block_size, total_size):
    """다운로드 진행 표시 — 매 0.5초마다 갱신."""
    global _last_print
    now = time.time()
    if now - _last_print < 0.5 and block * block_size < total_size:
        return
    _last_print = now
    downloaded = min(block * block_size, total_size) if total_size > 0 else block * block_size
    if total_size > 0:
        pct = downloaded * 100 / total_size
        print(f"  ⬇ {fmt_size(downloaded)} / {fmt_size(total_size)} ({pct:.1f}%)",
              flush=True)
    else:
        print(f"  ⬇ {fmt_size(downloaded)}", flush=True)


def main():
    print("=" * 60)
    print(" prepare_data — trades.db 준비")
    print("=" * 60)

    if os.path.exists(DB_PATH):
        sz = os.path.getsize(DB_PATH)
        print(f"✓ 이미 존재: {fmt_size(sz)}  → 다운로드 스킵")
        return 0

    print(f"⬇ 다운로드 시작")
    print(f"  URL: {DB_URL}")
    t0 = time.time()
    try:
        urllib.request.urlretrieve(DB_URL, DB_PATH, reporthook=_progress)
    except Exception as e:
        print(f"❌ 다운로드 실패: {type(e).__name__}: {e}")
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        return 1

    sz = os.path.getsize(DB_PATH)
    print(f"✓ 다운로드 완료: {fmt_size(sz)}  ({time.time() - t0:.1f}초)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
