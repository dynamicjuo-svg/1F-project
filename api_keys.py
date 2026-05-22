"""
API 키 로더 — 환경변수 우선, 로컬 .env 자동 로드.

로컬 개발:
  같은 디렉토리에 .env 파일 (이 파일이 자동 로드함, .gitignore로 제외됨)
  예시:
    MOLIT_KEY=xxx
    VWORLD_KEY=xxx
    ANTHROPIC_KEY=sk-ant-xxx
    NAVER_MAP_CLIENT_ID=xxx

호스팅 (Railway 등):
  Railway 콘솔의 Variables에 같은 4개 키를 환경변수로 등록 → 자동 사용
  (.env 없이 환경변수만으로 작동)
"""

import os
from pathlib import Path


def _load_env_file():
    """프로젝트 루트의 .env 파일을 환경변수로 로드 (있으면)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()


MOLIT_KEY = os.environ.get("MOLIT_KEY", "")
VWORLD_KEY = os.environ.get("VWORLD_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
NAVER_MAP_CLIENT_ID = os.environ.get("NAVER_MAP_CLIENT_ID", "")


if __name__ == "__main__":
    # 디버그용 — 어떤 키가 로드됐는지 확인 (값 자체는 출력 안 함)
    for name, value in [
        ("MOLIT_KEY", MOLIT_KEY),
        ("VWORLD_KEY", VWORLD_KEY),
        ("ANTHROPIC_KEY", ANTHROPIC_KEY),
        ("NAVER_MAP_CLIENT_ID", NAVER_MAP_CLIENT_ID),
    ]:
        mark = "✓" if value else "✗"
        print(f"  {mark} {name:24s}  ({len(value)}자 로드됨)")
