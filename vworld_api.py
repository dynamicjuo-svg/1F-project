"""
vworld_api.py — V-World API 호출 공통 wrapper.

V-World 인증키에 등록된 서비스 URL과 다른 도메인(localhost 등)에서 호출 시
Referer 헤더가 등록 도메인이어야 인증 통과.
"""

import requests
from api_keys import VWORLD_KEY


# 인증키 발급 시 등록된 서비스 URL
VWORLD_REFERER = "https://web-production-23a56.up.railway.app/"

DEFAULT_HEADERS = {
    "Referer": VWORLD_REFERER,
    "User-Agent": "Mozilla/5.0 (OneFamily/2.0)",
}


def get_feature(layer, attr_filter=None, geom_filter=None,
                 page=1, size=1000, geometry=True, format="json",
                 timeout=60, extra_params=None):
    """V-World GetFeature 호출. Referer 자동 적용.

    layer: 'LP_PA_CBND_BUBUN', 'LT_L_MOCTLINK', 'LT_C_UQ111' 등
    attr_filter: 'pnu:like:41461' 같은 속성 필터
    geom_filter: 'BOX(127.1,37.0,127.5,37.4)' 같은 bbox
    """
    params = {
        "service": "data",
        "request": "GetFeature",
        "data": layer,
        "key": VWORLD_KEY,
        "page": str(page),
        "size": str(size),
        "geometry": "true" if geometry else "false",
        "format": format,
    }
    if attr_filter:
        params["attrFilter"] = attr_filter
    if geom_filter:
        params["geomFilter"] = geom_filter
    if extra_params:
        params.update(extra_params)
    r = requests.get(
        "https://api.vworld.kr/req/data",
        params=params, headers=DEFAULT_HEADERS, timeout=timeout,
    )
    return r


def get_feature_json(layer, **kwargs):
    """get_feature + JSON 파싱. 응답 dict 반환. 에러면 'error' 키 포함."""
    try:
        r = get_feature(layer, **kwargs)
        return r.json()
    except Exception as e:
        return {"response": {"status": "EXCEPTION", "error": {"text": str(e)}}}


def iter_pages(layer, page_size=1000, max_pages=999, **kwargs):
    """페이지네이션 자동 — 모든 페이지의 features yield."""
    for page in range(1, max_pages + 1):
        d = get_feature_json(layer, page=page, size=page_size, **kwargs)
        resp = d.get("response", {})
        if resp.get("status") != "OK":
            err = resp.get("error", {})
            if page == 1:
                yield None, err  # 첫 호출 에러 알림용
            return
        result = resp.get("result", {})
        fc = result.get("featureCollection") or {}
        feats = fc.get("features", [])
        if not feats:
            return
        # 페이지 정보
        page_info = resp.get("page", {})
        total = int(page_info.get("total", "0") or 0)
        for ft in feats:
            yield ft, None
        if page >= total:
            return


if __name__ == "__main__":
    # 빠른 진단
    print(f"key: {VWORLD_KEY[:10]}...({len(VWORLD_KEY)}자)")
    print(f"referer: {VWORLD_REFERER}")
    print()
    print("=== 권한 확인 (작은 attr_filter로) ===")
    for layer in ["LP_PA_CBND_BUBUN", "LT_L_MOCTLINK", "LT_C_UQ111",
                  "LT_L_WTRTRA", "LT_C_NLSPB"]:
        r = get_feature(layer, attr_filter="sgg_cd:=:41461",
                        page=1, size=1, geometry=False)
        try:
            d = r.json()
            status = d.get("response", {}).get("status", "?")
            feats = (d.get("response", {}).get("result", {}).get(
                "featureCollection") or {}).get("features", [])
            err = (d.get("response", {}).get("error") or {}).get("code", "")
            print(f"  {layer:20s}: {status:5s}  feats={len(feats)}  {err}")
        except Exception:
            print(f"  {layer:20s}: BAD_JSON")
