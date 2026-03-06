"""
Scrapling-based scraping utilities.

Provides two main helpers:
  - fetch_static(url): HTTP-only fetch using Scrapling Fetcher (curl_cffi internally).
    No browser required. Memory ~5 MB per request. Use for sites that embed data
    in static HTML (Yahoo Shopping, Yahoo Auctions, Offmall, Surugaya).
  - fetch_dynamic(url): Browser-based fetch using Scrapling StealthyFetcher.
    Use for JS-heavy SPAs (Mercari, SNKRDUNK) as a future Selenium replacement.

Also provides _ScraplingSession and _ScraplingResponse as compatibility wrappers
so existing curl_cffi-based code (surugaya_db.py) can be migrated with minimal changes.
"""

import logging

logger = logging.getLogger("scraping_client")


class _ScraplingResponse:
    """
    Thin wrapper that makes a Scrapling Response look like a curl_cffi Response,
    preserving the .status_code, .text, .content, and .url interface.
    """

    def __init__(self, page):
        self._page = page

    @property
    def status_code(self) -> int:
        return int(getattr(self._page, "status", 200))

    @property
    def text(self) -> str:
        # Raw HTML string - used for Cloudflare/block marker detection
        body = self._page.body
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="ignore")
        return str(body or "")

    @property
    def content(self) -> bytes:
        # Raw bytes - used for BeautifulSoup parsing
        body = self._page.body
        if isinstance(body, bytes):
            return body
        return str(body or "").encode("utf-8")

    @property
    def url(self) -> str:
        return str(self._page.url or "")


class _ScraplingSession:
    """
    A session wrapper backed by Scrapling FetcherSession, providing a
    curl_cffi-compatible interface (.get(url, timeout=N)) with cookie persistence
    and stealthy Chrome impersonation headers generated automatically.
    """

    def __init__(self):
        from scrapling.engines.static import FetcherSession
        self._fs = FetcherSession(impersonate="chrome", stealthy_headers=True)
        self._inner = self._fs.__enter__()

    def get(self, url: str, timeout: int = 30) -> _ScraplingResponse:
        page = self._inner.get(url, timeout=timeout)
        return _ScraplingResponse(page)


def get_scraping_session() -> _ScraplingSession:
    """
    Create a new Scrapling-backed session with stealthy Chrome headers.
    Drop-in replacement for curl_cffi's requests.Session(impersonate='chrome120').
    """
    return _ScraplingSession()


def fetch_static(url: str, timeout: int = 30, **kwargs):
    """
    Fetch a URL using HTTP only (no browser). Backed by Scrapling Fetcher
    which uses curl_cffi with auto-generated stealthy Chrome headers.

    Returns a Scrapling Response (Adaptor) object supporting CSS selectors:
      page.find("#__NEXT_DATA__")
      page.css("script[type='application/ld+json']")
      el.text  /  el.attrib['src']

    Memory: ~5 MB per request (vs ~400 MB for Chrome).
    """
    from scrapling import Fetcher
    return Fetcher.get(url, stealthy_headers=True, timeout=timeout, **kwargs)


def fetch_dynamic(url: str, headless: bool = True, network_idle: bool = True, **kwargs):
    """
    Scrapling StealthyFetcher（Playwright ベース）でページ取得。

    前提: Dockerfile に `RUN python -m scrapling install` が追加済みであること。

    Args:
        url: 取得するURL
        headless: ヘッドレスモードで実行するか (default: True)
        network_idle: ネットワークアイドル待機するか (default: True)
    """
    from scrapling import StealthyFetcher
    return StealthyFetcher.fetch(
        url,
        headless=headless,
        network_idle=network_idle,
        **kwargs
    )
