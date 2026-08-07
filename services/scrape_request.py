"""
Shared scrape-request helpers used by routes and background workers.
"""
from __future__ import annotations

import ipaddress
import math
import re
from urllib.parse import urlencode, urlparse

from services.filter_service import normalize_price_bounds
from services.scrape_safety import (
    UnsafeScrapeUrlError,
    identify_marketplace_site,
    validate_marketplace_url,
)


SITE_LABELS = {
    "mercari": "メルカリ",
    "yahoo": "Yahoo!ショッピング",
    "rakuma": "ラクマ",
    "surugaya": "駿河屋",
    "offmall": "オフモール",
    "yahuoku": "ヤフオク",
    "snkrdunk": "SNKRDUNK",
    # Products read from a site outside the supported marketplaces.
    "other": "その他のサイト",
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


class InvalidTargetUrl(ValueError):
    """Raised when a pasted URL is not a safe, supported marketplace URL."""


def _normalize_hostname(hostname: str) -> str:
    host = str(hostname or "").strip().rstrip(".").lower()
    if not host:
        raise InvalidTargetUrl("URLのホスト名を確認してください。")
    try:
        normalized = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidTargetUrl("URLのホスト名を確認してください。") from exc
    labels = normalized.split(".")
    if (
        len(normalized) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        )
    ):
        raise InvalidTargetUrl("URLのホスト名を確認してください。")
    return normalized


def _parse_supported_target_url(url: str):
    raw = (url or "").strip()
    if not raw:
        raise InvalidTargetUrl("商品ページまたは検索結果ページのURLを入力してください。")

    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise InvalidTargetUrl("URLの形式を確認してください。") from exc

    if parsed.scheme.lower() != "https":
        raise InvalidTargetUrl("URLは https:// で始まる形式で入力してください。")
    if not parsed.netloc or not parsed.hostname:
        raise InvalidTargetUrl("URLのホスト名を確認してください。")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidTargetUrl("ユーザー名やパスワードを含むURLは使用できません。")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidTargetUrl("URLのポート番号を確認してください。") from exc
    if port not in (None, 443):
        raise InvalidTargetUrl("標準のHTTPSポート以外を指定したURLは使用できません。")

    host = _normalize_hostname(parsed.hostname)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InvalidTargetUrl("IPアドレスを直接指定したURLは使用できません。")

    site = identify_marketplace_site(host)
    if site:
        return parsed, host, site

    raise InvalidTargetUrl("対応している7サイトの商品ページまたは検索結果URLを入力してください。")


def detect_site_from_url(url: str) -> str:
    """Infer the scrape site from a target URL."""
    _parsed, _host, site = _parse_supported_target_url(url)
    return site


def classify_target_url(url: str) -> tuple[str, str]:
    """
    Classify a pasted target URL as ("item" | "search", site).

    Returns "item" for single-product pages and "search" for any other page
    on a known marketplace domain (search results, category and filtered
    listing pages). Unsafe or unsupported URLs raise InvalidTargetUrl.
    """
    _parsed, _host, site = _parse_supported_target_url(url)
    try:
        validate_marketplace_url(url, site, kind="detail")
    except UnsafeScrapeUrlError:
        pass
    else:
        return "item", site

    try:
        validate_marketplace_url(url, site, kind="search")
    except UnsafeScrapeUrlError as exc:
        raise InvalidTargetUrl(
            "対応サイトの商品ページまたは検索結果ページのURL形式を確認してください。"
        ) from exc
    return "search", site


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
        url_kind, _url_site = classify_target_url(target_url)
        if url_kind == "search":
            requested_limit = max(1, int(limit or 10))
            return {
                "site_label": "検索結果URLから抽出",
                "detail_label": simplify_target_label(target_url),
                "limit": requested_limit,
                "limit_label": f"{requested_limit}件",
                "persist_to_db": persist_to_db,
                "target_url": target_url,
                "keyword": keyword or "",
            }
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
