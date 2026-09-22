"""
Naver Real Estate Monitor

- Searches by exact complex name / aliases.
- Automatically discovers Naver complex IDs when not configured.
- Handles Naver 429 Too Many Requests with retries/backoff.
- Automatically creates output directories.
- Protects current.json from being overwritten by empty data
  when the crawl completely fails.

IMPORTANT:
- Naver's internal endpoints are unofficial and can change.
- No login/cookie/captcha bypass is used.
"""

import json
import random
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

# -----------------------------
# Rate-limit / retry settings
# -----------------------------

MAX_RETRIES = 5

# 요청 사이 기본 대기 시간
REQUEST_DELAY_SECONDS = 5

# 429 발생 시 최소 대기 시간
BACKOFF_BASE_SECONDS = 10

# 너무 빠른 반복 요청 방지
DISCOVERY_DELAY_SECONDS = 3


# -----------------------------
# HTTP helpers
# -----------------------------

session = requests.Session()
session.headers.update(HEADERS)


def request_json(url, params=None):
    """
    GET JSON with retry handling.

    Handles:
    - 429 Too Many Requests
    - 5xx temporary server errors
    """

    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(
                url,
                params=params,
                timeout=20,
            )

            # -----------------------------
            # Rate limit: 429
            # -----------------------------
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")

                if retry_after:
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        wait = BACKOFF_BASE_SECONDS * (2 ** attempt)
                else:
                    wait = BACKOFF_BASE_SECONDS * (2 ** attempt)

                # 약간의 random jitter
                wait += random.uniform(1, 3)

                print(
                    f"[429] Too Many Requests "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}) "
                    f"-> waiting {wait:.1f}s"
                )

                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue

                raise RuntimeError(
                    f"429 Too Many Requests after {MAX_RETRIES} retries"
                )

            # -----------------------------
            # Temporary server errors
            # -----------------------------
            if r.status_code in (500, 502, 503, 504):
                wait = BACKOFF_BASE_SECONDS * (attempt + 1)

                print(
                    f"[{r.status_code}] Temporary server error "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}) "
                    f"-> waiting {wait}s"
                )

                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue

                r.raise_for_status()

            # -----------------------------
            # Other HTTP errors
            # -----------------------------
            r.raise_for_status()

            return r.json()

        except requests.RequestException as e:
            if attempt >= MAX_RETRIES - 1:
                raise

            wait = BACKOFF_BASE_SECONDS * (attempt + 1)

            print(
                f"[REQUEST ERROR] {e} "
                f"(attempt {attempt + 1}/{MAX_RETRIES}) "
                f"-> waiting {wait}s"
            )

            time.sleep(wait)

    raise RuntimeError("request_json failed unexpectedly")


# -----------------------------
# Response parsing
# -----------------------------

def extract_articles(data):
    """
    Extract article list from possible Naver response structures.
    """

    if isinstance(data, dict):

        for key in (
            "articleList",
            "articles",
            "articleListData",
        ):
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
    """
    Return first non-empty value from dict.
    """

    for k in keys:
        v = d.get(k)

        if v not in (None, ""):
            return v

    return default


# -----------------------------
# Normalization
# -----------------------------

def normalize(a, complex_name, complex_type):
    """
    Normalize Naver article data into our own schema.
    """

    listing_id = str(
        first(
            a,
            [
                "articleNo",
                "atclNo",
                "articleNumber",
            ],
            "",
        )
    )

    return {
        "listing_id": listing_id,
        "complex": complex_name,
        "type": complex_type,

        "building": first(
            a,
            [
                "buildingName",
                "bildNm",
                "buildingNo",
            ],
        ),

        "floor": first(
            a,
            [
                "floorInfo",
                "flrInfo",
                "floor",
            ],
        ),

        "area_m2": first(
            a,
            [
                "area1",
                "spc1",
                "excluUseAr",
                "excluUseArTo",
            ],
        ),

        "price_krw_raw": first(
            a,
            [
                "dealOrWarrantPrc",
                "prcInfo",
                "price",
            ],
        ),

        "direction": first(
            a,
            [
                "direction",
                "directionInfo",
            ],
        ),

        "article_url": (
            f"https://fin.land.naver.com/articles/{listing_id}"
            if listing_id
            else ""
        ),

        "collected_at": datetime.now(
            timezone.utc
        ).isoformat(),

        # Keep original response for diagnostics
        "raw": a,
    }


# -----------------------------
# Naver complex API
# -----------------------------

def fetch_by_complex_id(complex_id, cfg):
    """
    Fetch apartment listings for a Naver complex.
    """

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


# -----------------------------
# Complex ID discovery
# -----------------------------

def discover_complex_page(query):
    """
    Discover complex ID using public Naver search.

    This is intentionally conservative.
    """

    url = "https://search.naver.com/search.naver"

    params = {
        "query": f"네이버 부동산 {query}"
    }

    r = session.get(
        url,
        params=params,
        timeout=20,
    )

    r.raise_for_status()

    ids = re.findall(
        r"/complexes/(\d+)",
        r.text,
    )

    return ids[0] if ids else None


# -----------------------------
# Safe JSON writer
# -----------------------------

def write_json(path, data):
    """
    Safely create parent directories and write JSON.
    """

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# -----------------------------
# Main
# -----------------------------

def main():

    print("========================================")
    print("Pyeongtaek Real Estate Monitor")
    print("========================================")

    # -----------------------------
    # Load config
    # -----------------------------

    config_path = Path("config.yaml")

    if not config_path.exists():
        raise FileNotFoundError(
            "config.yaml not found"
        )

    cfg = yaml.safe_load(
        config_path.read_text(
            encoding="utf-8"
        )
    )

    all_rows = []
    raw = {}

    total_complexes = len(
        cfg.get("complexes", [])
    )

    successful_complexes = 0
    failed_complexes = 0

    # -----------------------------
    # Crawl complexes
    # -----------------------------

    for index, c in enumerate(
        cfg["complexes"],
        start=1,
    ):

        complex_name = c["name"]

        print()
        print(
            f"[{index}/{total_complexes}] "
            f"{complex_name}"
        )

        cid = c.get("complex_id")

        # -----------------------------
        # Discover complex ID
        # -----------------------------

        if not cid:

            print(
                f"[DISCOVERY] Searching complex ID..."
            )

            for q in (
                [c["name"]]
                + c.get("aliases", [])
            ):

                try:

                    cid = discover_complex_page(q)

                    if cid:
                        print(
                            f"[DISCOVERY OK] "
                            f"{q} -> {cid}"
                        )
                        break

                except Exception as e:

                    raw[complex_name] = {
                        "discovery_error": str(e)
                    }

                    print(
                        f"[DISCOVERY ERROR] "
                        f"{q} -> {e}"
                    )

                # Avoid rapid search requests
                time.sleep(
                    DISCOVERY_DELAY_SECONDS
                )

        # -----------------------------
        # Could not find complex ID
        # -----------------------------

        if not cid:

            raw[complex_name] = {
                "status":
                "complex_id_not_discovered"
            }

            print(
                f"[UNRESOLVED] "
                f"{complex_name}"
            )

            failed_complexes += 1

            continue

        # -----------------------------
        # Fetch listings
        # -----------------------------

        try:

            print(
                f"[CRAWL] "
                f"{complex_name} -> {cid}"
            )

            data = fetch_by_complex_id(
                cid,
                cfg,
            )

            articles = extract_articles(
                data
            )

            raw[complex_name] = {
                "complex_id": cid,
                "article_count": len(articles),
                "response": data,
            }

            for a in articles:

                all_rows.append(
                    normalize(
                        a,
                        complex_name,
                        c["type"],
                    )
                )

            print(
                f"[OK] {complex_name} "
                f"-> {cid}: "
                f"{len(articles)} articles"
            )

            successful_complexes += 1

        except Exception as e:

            raw[complex_name] = {
                "complex_id": cid,
                "error": str(e),
            }

            print(
                f"[ERROR] "
                f"{complex_name} -> {e}"
            )

            failed_complexes += 1

        # -----------------------------
        # Delay between complexes
        # -----------------------------

        if index < total_complexes:

            delay = (
                REQUEST_DELAY_SECONDS
                + random.uniform(1, 3)
            )

            print(
                f"[WAIT] "
                f"{delay:.1f}s before next complex"
            )

            time.sleep(delay)

    # -----------------------------
    # Output paths
    # -----------------------------

    current_path = Path(
        cfg["output"]["current"]
    )

    raw_path = Path(
        cfg["output"]["raw"]
    )

    # Automatically create data directory
    current_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    raw_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # -----------------------------
    # Save raw diagnostic data
    # -----------------------------

    write_json(
        raw_path,
        raw,
    )

    # -----------------------------
    # EMPTY DATA PROTECTION
    # -----------------------------

    if not all_rows:

        print()
        print("========================================")
        print("WARNING: No articles collected")
        print("========================================")

        print(
            f"Successful complexes: "
            f"{successful_complexes}"
        )

        print(
            f"Failed complexes: "
            f"{failed_complexes}"
        )

        # IMPORTANT:
        # Do NOT overwrite current.json
        # with [] if the entire crawl failed.

        if current_path.exists():

            print(
                "[PROTECTED] "
                "Existing current.json preserved."
            )

        else:

            # First run: create empty file so
            # downstream steps do not crash.
            write_json(
                current_path,
                [],
            )

            print(
                "[CREATED] "
                "Empty current.json "
                "(first run)"
            )

    else:

        # -----------------------------
        # Normal successful save
        # -----------------------------

        write_json(
            current_path,
            all_rows,
        )

        print()
        print(
            f"[SAVED] "
            f"{len(all_rows)} articles"
        )

    # -----------------------------
    # Final summary
    # -----------------------------

    print()
    print("========================================")
    print("Crawl finished")
    print("========================================")

    print(
        f"Complexes: {total_complexes}"
    )

    print(
        f"Successful: {successful_complexes}"
    )

    print(
        f"Failed: {failed_complexes}"
    )

    print(
        f"Articles: {len(all_rows)}"
    )

    print(
        f"Current: {current_path}"
    )

    print(
        f"Raw: {raw_path}"
    )

    print("========================================")


if __name__ == "__main__":
    main()
