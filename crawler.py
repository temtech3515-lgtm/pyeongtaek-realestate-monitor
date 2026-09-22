"""
Naver Real Estate monitor.
This version searches by exact complex name/aliases rather than requiring
a manually entered complex ID for every Gajae complex.

IMPORTANT:
- Naver's internal endpoints are unofficial and can change.
- No login/cookie/captcha bypass is used.
- The search endpoint and normalization are isolated so they can be updated.
"""
import json, re, time
from pathlib import Path
from datetime import datetime, timezone
import requests, yaml

BASE = "https://new.land.naver.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://new.land.naver.com/",
}

def request_json(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()

def extract_articles(data):
    if isinstance(data, dict):
        for key in ("articleList", "articles", "articleListData"):
            if isinstance(data.get(key), list):
                return data[key]
        # nested response
        for v in data.values():
            if isinstance(v, dict):
                a = extract_articles(v)
                if a:
                    return a
    return []

def first(d, keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default

def normalize(a, complex_name, complex_type):
    listing_id = str(first(a, ["articleNo", "atclNo", "articleNumber"], ""))
    return {
        "listing_id": listing_id,
        "complex": complex_name,
        "type": complex_type,
        "building": first(a, ["buildingName", "bildNm", "buildingNo"]),
        "floor": first(a, ["floorInfo", "flrInfo", "floor"]),
        "area_m2": first(a, ["area1", "spc1", "excluUseAr", "excluUseArTo"]),
        "price_krw_raw": first(a, ["dealOrWarrantPrc", "prcInfo", "price"]),
        "direction": first(a, ["direction", "directionInfo"]),
        "article_url": f"https://fin.land.naver.com/articles/{listing_id}" if listing_id else "",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "raw": a,
    }

def fetch_by_complex_id(complex_id, cfg):
    # Known Naver complex endpoint pattern.
    params = {
        "realEstateType": cfg["search"]["real_estate_type"],
        "tradeType": cfg["search"]["trade_type"],
        "priceMin": 0,
        "priceMax": 990000000,
        "areaMin": cfg["search"]["area_min_m2"],
        "areaMax": cfg["search"]["area_max_m2"],
        "priceType": "RETAIL",
        "page": 1,
        "complexNo": complex_id,
    }
    return request_json(f"{BASE}/api/articles/complex/{complex_id}", params=params)

def discover_complex_page(query):
    # Public search page fallback. The HTML is retained for diagnostics;
    # extracting an ID from it is intentionally conservative.
    url = "https://search.naver.com/search.naver"
    params = {"query": f"네이버 부동산 {query}"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    ids = re.findall(r"/complexes/(\d+)", r.text)
    return ids[0] if ids else None

def main():
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    all_rows, raw = [], {}

    for c in cfg["complexes"]:
        cid = c.get("complex_id")
        if not cid:
            for q in [c["name"]] + c.get("aliases", []):
                try:
                    cid = discover_complex_page(q)
                    if cid:
                        break
                except Exception as e:
                    raw[c["name"]] = {"discovery_error": str(e)}
                time.sleep(1)
        if not cid:
            raw[c["name"]] = {"status": "complex_id_not_discovered"}
            print(f"[UNRESOLVED] {c['name']}")
            continue
        try:
            data = fetch_by_complex_id(cid, cfg)
            raw[c["name"]] = {"complex_id": cid, "response": data}
            for a in extract_articles(data):
                all_rows.append(normalize(a, c["name"], c["type"]))
            print(f"[OK] {c['name']} -> {cid}: {len(extract_articles(data))} articles")
        except Exception as e:
            raw[c["name"]] = {"complex_id": cid, "error": str(e)}
            print(f"[ERROR] {c['name']} -> {e}")
        time.sleep(1.5)

    Path(cfg["output"]["current"]).write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(cfg["output"]["raw"]).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
