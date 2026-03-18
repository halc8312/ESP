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

import asyncio
import logging
import os
import queue
import threading
from dataclasses import dataclass

logger = logging.getLogger("scraping_client")


@dataclass(frozen=True)
class AsyncFetchSettings:
    concurrency: int
    timeout: int
    retries: int
    backoff_seconds: float


_ASYNC_FETCH_DEFAULTS = {
    "default": AsyncFetchSettings(concurrency=3, timeout=30, retries=1, backoff_seconds=0.75),
    "mercari": AsyncFetchSettings(concurrency=1, timeout=30, retries=0, backoff_seconds=0.0),
    "rakuma": AsyncFetchSettings(concurrency=4, timeout=20, retries=1, backoff_seconds=0.5),
    "snkrdunk": AsyncFetchSettings(concurrency=4, timeout=20, retries=1, backoff_seconds=0.5),
}


def _get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r. Falling back to %s.", name, raw, default)
        return default


def _get_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r. Falling back to %s.", name, raw, default)
        return default


def get_async_fetch_settings(site: str) -> AsyncFetchSettings:
    site_key = str(site or "").strip().lower()
    defaults = _ASYNC_FETCH_DEFAULTS.get(site_key, _ASYNC_FETCH_DEFAULTS["default"])
    env_prefix = site_key.upper() if site_key else "SCRAPE"

    return AsyncFetchSettings(
        concurrency=max(1, _get_env_int(f"{env_prefix}_DETAIL_CONCURRENCY", defaults.concurrency)),
        timeout=max(1, _get_env_int(f"{env_prefix}_DETAIL_TIMEOUT", defaults.timeout)),
        retries=max(0, _get_env_int(f"{env_prefix}_DETAIL_RETRIES", defaults.retries)),
        backoff_seconds=max(0.0, _get_env_float(f"{env_prefix}_DETAIL_BACKOFF", defaults.backoff_seconds)),
    )


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


async def fetch_static_async(
    url: str,
    timeout: int = 30,
    retries: int = 0,
    backoff_seconds: float = 0.0,
    **kwargs,
):
    """
    Async HTTP fetch backed by Scrapling AsyncFetcher.

    Used for SSR / JSON-in-HTML sites where browser startup is unnecessary.
    Retries are intentionally lightweight so call sites can fan out with
    `asyncio.gather` without turning transient failures into hard aborts.
    """
    from scrapling.fetchers import AsyncFetcher

    last_error = None
    for attempt in range(retries + 1):
        try:
            return await AsyncFetcher.get(url, stealthy_headers=True, timeout=timeout, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            delay = backoff_seconds * (attempt + 1)
            if delay > 0:
                await asyncio.sleep(delay)

    raise last_error


async def gather_with_concurrency(values, worker, concurrency: int, return_exceptions: bool = True):
    """
    Run async workers with bounded concurrency while preserving input order.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results = [None] * len(values)

    async def _run_one(index, value):
        async with semaphore:
            try:
                results[index] = await worker(value)
            except Exception as exc:
                if return_exceptions:
                    results[index] = exc
                    return
                raise

    await asyncio.gather(*(_run_one(index, value) for index, value in enumerate(values)))
    return results


def run_coro_sync(coro):
    """
    Run a coroutine from synchronous code.

    If the current thread already owns a running event loop, execute the
    coroutine inside a worker thread with its own loop so sync wrappers remain
    callable from loop-aware test environments and future worker runtimes.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_queue = queue.Queue(maxsize=1)

    def _runner():
        try:
            result_queue.put((True, asyncio.run(coro)))
        except Exception as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    ok, payload = result_queue.get()
    if ok:
        return payload
    raise payload


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
