"""
Utility functions for the application.
"""
import re
from urllib.parse import urlsplit, urlunsplit


def normalize_url(raw_url: str) -> str:
    """?以降のクエリを落として正規化したURLを返す"""
    try:
        parts = urlsplit(raw_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return raw_url


# ---------------------------------------------------------------------------
# URL validation: reject search/listing pages that patrol cannot handle
# ---------------------------------------------------------------------------

# Per-site regex patterns that a *detail page* URL must match.
# If a URL matches the host but NOT the detail pattern, it is rejected.
_DETAIL_URL_PATTERNS: dict[str, re.Pattern] = {
    # mercari: https://jp.mercari.com/item/m12345
    "mercari": re.compile(r"jp\.mercari\.com/item/m[\w-]+", re.I),
    # yahoo shopping:
    #   https://store.shopping.yahoo.co.jp/STORENAME/ITEMID.html
    #   https://shopping.yahoo.co.jp/products/ITEMID  (product page)
    # Reject: https://shopping.yahoo.co.jp/search/...
    "yahoo": re.compile(
        r"shopping\.yahoo\.co\.jp/(?!search[/?])", re.I
    ),
    # rakuma (fril):
    #   https://item.fril.jp/<hash>
    #   https://fril.jp/product/<id>
    "rakuma": re.compile(r"fril\.jp/(product/)?\w+", re.I),
    # surugaya: https://www.suruga-ya.jp/product/detail/<code>
    "surugaya": re.compile(r"suruga-ya\.jp/product/detail/", re.I),
    # offmall:
    #   https://netmall.hardoff.co.jp/product/<id>/
    #   https://offmall.hardoff.co.jp/.../<id>
    "offmall": re.compile(r"(netmall|offmall)\.hardoff\.co\.jp/.+/.+", re.I),
    # yahuoku: https://page.auctions.yahoo.co.jp/jp/auction/<id>
    "yahuoku": re.compile(
        r"(page\.auctions\.yahoo\.co\.jp|auctions\.yahoo\.co\.jp/jp/auction)", re.I
    ),
    # snkrdunk: https://snkrdunk.com/products/<slug>
    "snkrdunk": re.compile(r"snkrdunk\.com/products/", re.I),
}

# Generic patterns that indicate a *search / listing* page regardless of site
_SEARCH_INDICATORS = re.compile(
    r"[?&](keyword|query|q|search|p)=", re.I
)


def is_valid_detail_url(url: str, site: str) -> bool:
    """
    Return True if *url* looks like a product detail page for *site*.

    Rejects:
      - empty / whitespace-only URLs
      - URLs containing search query parameters
      - URLs that don't match the expected detail-page pattern for the site
    """
    if not url or not url.strip():
        return False

    # Reject obvious search / listing URLs
    if _SEARCH_INDICATORS.search(url):
        return False

    pattern = _DETAIL_URL_PATTERNS.get(site)
    if pattern is None:
        # Unknown site – allow through (don't break future sites)
        return True

    return bool(pattern.search(url))

