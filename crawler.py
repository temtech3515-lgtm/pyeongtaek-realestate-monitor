"""
Naver Real Estate monitor.

- Searches by exact complex name/aliases.
- Uses Naver's unofficial public endpoints.
- No login/cookie/captcha bypass.
- Handles Naver 429 responses with exponential backoff.
- Creates output directories automatically.
"""

import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
import yaml


BASE = "https://new.land.naver.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://new.land.naver.com/",
}


def request_json(url, params=None, max_retries=4):
    """
    Request JSON with retry/backoff for Naver rate limiting.

    429 -> wait and retry
    Other HTTP errors -> raise immediately
    """

    for attempt in range(max_retries):
        try:
            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=20,
            )

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")

                if retry_after:
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        wait = 30
                else:
                    wait = 20 * (2 ** attempt)

                print(
                    f"[429] Too Many Requests. "
                    f"Retry {attempt + 1}/{max_retries} "
                    f"after {wait}s"
                )

                time.sleep(wait)
                continue

            r.raise_for_status()
            return r.json()

        except requests.RequestException:
            if attempt == max_retries - 1:
                raise

            wait = 10 * (2 ** attempt)

            print(
                f"[REQUEST ERROR] "
                f"Retry {attempt + 1}/{max_retries} "
                f"after {wait}s"
            )

            time.sleep(wait)

    raise RuntimeError(f"Request failed after {max_retries} retries: {url}")


def extract_articles(data):
    if isinstance(data, dict):
        for key in ("articleList", "articles", "articleListData"):
            if isinstance(data.get(key), list):
                return data[key]

        # Nested response
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
    listing_id = str(
        first(
            a,
            ["articleNo", "atclNo", "articleNumber"],
            "",
        )
    )

    return {
        "listing_id": listing_id,
        "complex": complex_name,
        "type": complex_type,
        "building": first(
            a,
            ["buildingName", "bildNm", "buildingNo"],
        ),
        "floor": first(
            a,
            ["floorInfo", "flrInfo", "floor"],
        ),
        "area_m2": first(
            a,
            ["area1", "spc1", "excluUseAr", "excluUseArTo"],
        ),
        "price_krw_raw": first(
            a,
            ["dealOrWarrantPrc", "prcInfo", "price"],
        ),
        "direction": first(
            a,
            ["direction", "directionInfo"],
        ),
        "article_url": (
            f"https://fin.land.naver.com/articles/{listing_id}"
            if listing_id
            else ""
        ),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "raw": a,
    }


def fetch_by_complex_id(complex_id, cfg):
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

    return request_json(
        f"{BASE}/api/articles/complex/{complex_id}",
        params=params,
    )


def discover_complex_page(query):
    """
    Search Naver for a complex ID.
    """

    url = "https://search.naver.com/search.naver"

    params = {
        "query": f"네이버 부동산 {query}"
    }

    r = requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=20,
    )

    if r.status_code == 429:
        print(
            f"[429] Discovery rate limited for '{query}'. "
            f"Skipping discovery."
        )
        return None

    r.raise_for_status()

    ids = re.findall(
        r"/complexes/(\d+)",
        r.text,
    )

    return ids[0] if ids else None


def main():

    cfg = yaml.safe_load(
        Path("config.yaml").read_text(
            encoding="utf-8"
        )
    )

    all_rows = []
    raw = {}

    for c in cfg["complexes"]:

        cid = c.get("complex_id")

        # -------------------------------------------------
        # 1. Complex ID discovery
        # -------------------------------------------------

        if not cid:

            for q in [c["name"]] + c.get("aliases", []):

                try:
                    cid = discover_complex_page(q)

                    if cid:
                        print(
                            f"[DISCOVERED] "
                            f"{c['name']} -> {cid}"
                        )
                        break

                except Exception as e:
                    print(
                        f"[DISCOVERY ERROR] "
                        f"{c['name']} / {q} -> {e}"
                    )

                # Discovery requests are intentionally slow.
                time.sleep(3)

        # -------------------------------------------------
        # 2. Could not find complex ID
        # -------------------------------------------------

        if not cid:

            raw[c["name"]] = {
                "status": "complex_id_not_discovered"
            }

            print(
                f"[UNRESOLVED] {c['name']}"
            )

            continue

        # -------------------------------------------------
        # 3. Fetch listings
        # -------------------------------------------------

        try:

            data = fetch_by_complex_id(
                cid,
                cfg,
            )

            articles = extract_articles(data)

            raw[c["name"]] = {
                "complex_id": cid,
                "article_count": len(articles),
                "response": data,
            }

            for a in articles:

                all_rows.append(
                    normalize(
                        a,
                        c["name"],
                        c["type"],
                    )
                )

            print(
                f"[OK] {c['name']} -> "
                f"{cid}: {len(articles)} articles"
            )

        except Exception as e:

            raw[c["name"]] = {
                "complex_id": cid,
                "error": str(e),
            }

            print(
                f"[ERROR] {c['name']} -> {e}"
            )

        # -------------------------------------------------
        # 4. Slow down between complexes
        # -------------------------------------------------

        time.sleep(5)

    # -----------------------------------------------------
    # 5. Make output directories automatically
    # -----------------------------------------------------

    current_path = Path(
        cfg["output"]["current"]
    )

    raw_path = Path(
        cfg["output"]["raw"]
    )

    current_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    raw_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # -----------------------------------------------------
    # 6. Save current data
    # -----------------------------------------------------

    current_path.write_text(
        json.dumps(
            all_rows,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # -----------------------------------------------------
    # 7. Save raw data / diagnostics
    # -----------------------------------------------------

    raw_path.write_text(
        json.dumps(
            raw,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"[DONE] "
        f"{len(all_rows)} listings collected."
    )

    print(
        f"[OUTPUT] {current_path}"
    )

    print(
        f"[OUTPUT] {raw_path}"
    )


if __name__ == "__main__":
    main()
