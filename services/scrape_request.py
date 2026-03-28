"""
Shared scrape-request helpers used by routes and background workers.
"""
from __future__ import annotations

import math
from urllib.parse import urlencode, urlparse

from services.filter_service import normalize_price_bounds


DOMAIN_SITE_MAP = [
    ("fril.jp", "rakuma"),
    ("item.fril.jp", "rakuma"),
    ("jp.mercari.com", "mercari"),
    ("shopping.yahoo.co.jp", "yahoo"),
    ("suruga-ya.jp", "surugaya"),
    ("netmall.hardoff.co.jp", "offmall"),
    ("auctions.yahoo.co.jp", "yahuoku"),
    ("snkrdunk.com", "snkrdunk"),
]

SITE_LABELS = {
    "mercari": "メルカリ",
    "yahoo": "Yahoo!ショッピング",
    "rakuma": "ラクマ",
    "surugaya": "駿河屋",
    "offmall": "オフモール",
    "yahuoku": "ヤフオク",
    "snkrdunk": "SNKRDUNK",
}

SEARCH_DEPTH_RULES = {
    "mercari": {"window": 16, "base": 2, "min": 3, "max": 10},
    "rakuma": {"window": 18, "base": 2, "min": 3, "max": 10},
    "yahoo": {"window": 24, "base": 2, "min": 3, "max": 8},
    "surugaya": {"window": 18, "base": 2, "min": 3, "max": 7},
    "offmall": {"window": 24, "base": 2, "min": 3, "max": 8},
    "yahuoku": {"window": 24, "base": 2, "min": 3, "max": 8},
    "snkrdunk": {"window": 20, "base": 2, "min": 3, "max": 8},
}


def detect_site_from_url(url: str) -> str:
    """Infer the scrape site from a target URL."""
    for domain, site in DOMAIN_SITE_MAP:
        if domain in url:
            return site
    return "mercari"


def get_internal_search_limit(limit: int) -> int:
    """Expand the crawl window to absorb exclusions and fetch failures."""
    requested = max(1, int(limit or 10))
    if requested <= 10:
        return requested
    return min(150, max(requested + 10, int(math.ceil(requested * 1.4))))


def get_search_depth(site: str, limit: int) -> int:
    """Return a site-aware depth value for the requested item count."""
    requested = max(1, int(limit or 10))
    rule = SEARCH_DEPTH_RULES.get(site, {"window": 20, "base": 2, "min": 3, "max": 8})
    depth = int(math.ceil(requested / rule["window"])) + rule["base"]
    return max(rule["min"], min(depth, rule["max"]))


def simplify_target_label(target_url: str) -> str:
    if not target_url:
        return "URL指定"
    try:
        parsed = urlparse(target_url)
        return parsed.hostname or "URL指定"
    except ValueError:
        return "URL指定"


def build_scrape_job_context(
    site: str,
    target_url: str | None,
    keyword: str | None,
    limit: int,
    persist_to_db: bool,
) -> dict[str, object]:
    if target_url:
        return {
            "site_label": "URLから抽出",
            "detail_label": simplify_target_label(target_url),
            "limit": 1,
            "limit_label": "1件",
            "persist_to_db": persist_to_db,
            "target_url": target_url,
            "keyword": keyword or "",
        }

    requested_limit = max(1, int(limit or 10))
    return {
        "site_label": SITE_LABELS.get(site, "商品抽出"),
        "detail_label": f"キーワード: {keyword}" if keyword else "条件で抽出します",
        "limit": requested_limit,
        "limit_label": f"{requested_limit}件",
        "persist_to_db": persist_to_db,
        "target_url": "",
        "keyword": keyword or "",
    }


def build_search_url(
    site: str,
    keyword: str | None,
    price_min,
    price_max,
    sort: str | None,
    category,
) -> str:
    """
    Build a site-aware search URL.

    Native price params are added where the current site exposes a verified URL
    contract. The common post-scrape price filter remains as defense-in-depth.
    """
    min_value, max_value = normalize_price_bounds(price_min, price_max)
    min_str = str(min_value) if min_value is not None else None
    max_str = str(max_value) if max_value is not None else None

    keyword = (keyword or "").strip()
    sort = (sort or "").strip()
    category = (category or "").strip()

    if site == "yahoo":
        params = {}
        if keyword:
            params["p"] = keyword
        if min_str:
            params["pf"] = min_str
        if max_str:
            params["pt"] = max_str
        return "https://shopping.yahoo.co.jp/search?" + urlencode(params)

    if site == "rakuma":
        params = {}
        if keyword:
            params["query"] = keyword
        if min_str:
            params["min"] = min_str
        if max_str:
            params["max"] = max_str
        return "https://fril.jp/s?" + urlencode(params)

    if site == "surugaya":
        params = {}
        if keyword:
            params["search_word"] = keyword
            params["is_stock"] = "1"
        if min_str or max_str:
            params["price"] = f"[{min_str or 0},{max_str or '*'}]"
        return "https://www.suruga-ya.jp/search?" + urlencode(params)

    if site == "offmall":
        params = {}
        if keyword:
            params["q"] = keyword
        if min_str:
            params["min"] = min_str
        if max_str:
            params["max"] = max_str
        return "https://netmall.hardoff.co.jp/search/?" + urlencode(params)

    if site == "yahuoku":
        params = {}
        if keyword:
            params["p"] = keyword
            params["va"] = keyword
        if min_str:
            params["aucminprice"] = min_str
        if max_str:
            params["aucmaxprice"] = max_str
        return "https://auctions.yahoo.co.jp/search/search?" + urlencode(params)

    if site == "snkrdunk":
        params = {}
        if keyword:
            params["keywords"] = keyword
        if min_str:
            params["minPrice"] = min_str
        if max_str:
            params["maxPrice"] = max_str
        return "https://snkrdunk.com/search?" + urlencode(params)

    params = {}
    if keyword:
        params["keyword"] = keyword
    if min_str:
        params["price_min"] = min_str
    if max_str:
        params["price_max"] = max_str
    if sort:
        params["sort"] = sort
    if category:
        params["category_id"] = category
    return "https://jp.mercari.com/search?" + urlencode(params)


def build_scrape_task_request(
    site: str,
    target_url: str | None,
    keyword: str | None,
    price_min,
    price_max,
    sort: str | None,
    category,
    limit: int,
    user_id: int | None,
    persist_to_db: bool = True,
    shop_id: int | None = None,
) -> dict[str, object]:
    return {
        "site": site,
        "target_url": target_url or "",
        "keyword": keyword or "",
        "price_min": price_min,
        "price_max": price_max,
        "sort": sort or "",
        "category": category,
        "limit": max(1, int(limit or 10)),
        "user_id": user_id,
        "persist_to_db": persist_to_db,
        "shop_id": shop_id,
    }
