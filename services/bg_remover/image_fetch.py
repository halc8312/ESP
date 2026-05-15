"""
Shared helpers for fetching source images during background removal.

Both the inline execution path (web process) and the RQ worker path
need to download the original product image before running the
bg-removal backend.  External marketplace CDNs (Mercari, snkrdunk,
surugaya …) reject requests that lack plausible browser headers, so
every fetch must include at least a ``User-Agent`` and, where
applicable, the correct ``Referer``.
"""
from __future__ import annotations

from urllib.parse import urlparse


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_REFERER_BY_DOMAIN: list[tuple[str, str]] = [
    ("mercdn.net", "https://jp.mercari.com/"),
    ("mercari.com", "https://jp.mercari.com/"),
    ("fril.jp", "https://fril.jp/"),
    ("snkrdunk.com", "https://snkrdunk.com/"),
    ("suruga-ya.jp", "https://www.suruga-ya.jp/"),
]


def build_image_fetch_headers(url: str) -> dict[str, str]:
    """Return request headers suitable for fetching *url* from a CDN."""
    headers: dict[str, str] = {"User-Agent": _DEFAULT_USER_AGENT}
    hostname = (urlparse(url).hostname or "").lower()
    for domain_suffix, referer in _REFERER_BY_DOMAIN:
        if hostname == domain_suffix or hostname.endswith("." + domain_suffix):
            headers["Referer"] = referer
            break
    return headers
