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
import base64
import inspect
import logging
import os
import queue
import threading
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse, urlunparse

logger = logging.getLogger("scraping_client")


@dataclass(frozen=True)
class AsyncFetchSettings:
    concurrency: int
    timeout: int
    retries: int
    backoff_seconds: float


@dataclass(frozen=True)
class ExternalFetchResponse:
    url: str
    status_code: int
    text: str
    source: str
    transport_url: str = ""

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def body(self) -> str:
        return self.text

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    @property
    def headers(self) -> dict:
        return {}


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

    @property
    def headers(self):
        return getattr(self._page, "headers", {}) or {}


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

    def get(self, url: str, timeout: int = 30, **kwargs) -> _ScraplingResponse:
        if "allow_redirects" in kwargs and "follow_redirects" not in kwargs:
            kwargs["follow_redirects"] = kwargs.pop("allow_redirects")
        page = self._inner.get(url, timeout=timeout, **kwargs)
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


def _is_test_fetch_double(fetcher) -> bool:
    module_name = str(getattr(fetcher, "__module__", "") or "")
    return (
        module_name.startswith("test_")
        or module_name.startswith("tests.")
        or module_name.startswith("unittest.mock")
    )


def _call_static_without_redirects(fetcher, url: str, *, timeout: int, kwargs: dict):
    call_kwargs = dict(kwargs)
    call_kwargs["timeout"] = timeout
    call_kwargs["follow_redirects"] = False
    try:
        return fetcher(url, **call_kwargs)
    except TypeError:
        if not _is_test_fetch_double(fetcher):
            raise
        # Lightweight unit-test doubles cannot redirect and often accept only
        # the URL positional argument.
        signature = inspect.signature(fetcher)
        if "timeout" in signature.parameters:
            return fetcher(url, timeout=timeout)
        return fetcher(url)


def _safe_transport_origin(url: str) -> str:
    """Return a credential/query/path-free origin suitable for diagnostics."""
    try:
        parsed = urlparse(str(url or ""))
        host = str(parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def _call_external_transport(source: str, operation):
    """Run a provider request without surfacing credential-bearing URLs."""
    try:
        return operation()
    except Exception as exc:
        raise RuntimeError(
            f"Surugaya external fetch via {source} failed "
            f"({type(exc).__name__})."
        ) from None


def fetch_marketplace_static(
    url: str,
    *,
    site: str,
    kind: str,
    timeout: int = 30,
    max_redirects: int = 5,
    allowed_statuses: frozenset[int] = frozenset(),
    **kwargs,
):
    """Fetch a marketplace page without permitting unvalidated redirects."""
    from services.scrape_safety import fetch_with_safe_redirects

    return fetch_with_safe_redirects(
        lambda current_url: _call_static_without_redirects(
            fetch_static,
            current_url,
            timeout=timeout,
            kwargs=kwargs,
        ),
        url,
        site,
        kind=kind,
        max_redirects=max_redirects,
        allowed_statuses=allowed_statuses,
    )


def fetch_surugaya_external(url: str, timeout: int = 60) -> ExternalFetchResponse | None:
    from curl_cffi import requests
    from services.scrape_safety import (
        ScrapeFailure,
        fetch_with_safe_redirects,
        validate_marketplace_url,
    )

    target_kind = "detail" if "/product/detail/" in url else "search"
    url = validate_marketplace_url(url, "surugaya", kind=target_kind)

    zyte_key = (os.environ.get("SURUGAYA_ZYTE_API_KEY") or "").strip()
    if zyte_key:
        token = base64.b64encode(f"{zyte_key}:".encode("utf-8")).decode("ascii")
        response = _call_external_transport(
            "zyte",
            lambda: requests.post(
                "https://api.zyte.com/v1/extract",
                headers={"Authorization": f"Basic {token}"},
                json={"url": url, "browserHtml": True, "geolocation": "JP"},
                timeout=timeout,
                allow_redirects=False,
            ),
        )
        if response.status_code >= 400:
            return ExternalFetchResponse(
                url=url,
                status_code=response.status_code,
                text=response.text,
                source="zyte",
                transport_url="https://api.zyte.com",
            )
        data = _call_external_transport("zyte", response.json)
        html = str(data.get("browserHtml") or data.get("httpResponseBody") or "")
        if html:
            status_code = data.get("browserHtmlStatusCode") or data.get("statusCode") or 200
            return ExternalFetchResponse(
                url=url,
                status_code=int(status_code),
                text=html,
                source="zyte",
                transport_url="https://api.zyte.com",
            )

    scraperapi_key = (os.environ.get("SURUGAYA_SCRAPERAPI_KEY") or "").strip()
    if scraperapi_key:
        response = _call_external_transport(
            "scraperapi",
            lambda: requests.get(
                "https://api.scraperapi.com",
                params={
                    "api_key": scraperapi_key,
                    "url": url,
                    "render": "true",
                    "country_code": "jp",
                },
                timeout=timeout,
                allow_redirects=False,
            ),
        )
        return ExternalFetchResponse(
            url=url,
            status_code=response.status_code,
            text=response.text,
            source="scraperapi",
            transport_url="https://api.scraperapi.com",
        )

    template = (os.environ.get("SURUGAYA_FETCH_API_URL_TEMPLATE") or "").strip()
    if template:
        fetch_url = template.format(url=quote_plus(url), raw_url=url)
        response = _call_external_transport(
            "template",
            lambda: requests.get(
                fetch_url,
                timeout=timeout,
                allow_redirects=False,
            ),
        )
        return ExternalFetchResponse(
            url=url,
            status_code=response.status_code,
            text=response.text,
            source="template",
            transport_url=_safe_transport_origin(fetch_url),
        )

    proxy_url = (os.environ.get("SURUGAYA_PROXY_URL") or "").strip()
    if proxy_url:
        try:
            response = fetch_with_safe_redirects(
                lambda current_url: requests.get(
                    current_url,
                    timeout=timeout,
                    impersonate="chrome120",
                    proxies={"http": proxy_url, "https": proxy_url},
                    headers={"Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"},
                    allow_redirects=False,
                ),
                url,
                "surugaya",
                kind=target_kind,
            )
        except ScrapeFailure:
            raise
        except Exception as exc:
            raise RuntimeError(
                "Surugaya external fetch via proxy failed "
                f"({type(exc).__name__})."
            ) from None
        return ExternalFetchResponse(
            url=response.url or url,
            status_code=response.status_code,
            text=response.text,
            source="proxy",
            transport_url=_safe_transport_origin(proxy_url),
        )

    return None


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


async def fetch_marketplace_static_async(
    url: str,
    *,
    site: str,
    kind: str,
    timeout: int = 30,
    retries: int = 0,
    backoff_seconds: float = 0.0,
    max_redirects: int = 5,
    allowed_statuses: frozenset[int] = frozenset(),
    **kwargs,
):
    """Async static fetch with per-hop marketplace redirect validation."""
    from services.scrape_safety import fetch_with_safe_redirects_async

    async def _fetch_once(current_url: str):
        return await fetch_static_async(
            current_url,
            timeout=timeout,
            retries=retries,
            backoff_seconds=backoff_seconds,
            follow_redirects=False,
            **kwargs,
        )

    return await fetch_with_safe_redirects_async(
        _fetch_once,
        url,
        site,
        kind=kind,
        max_redirects=max_redirects,
        allowed_statuses=allowed_statuses,
    )


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
        allowed_statuses: 失敗として扱わないHTTPステータス。巡回のように
            404を「削除済み」と解釈したい呼び出し元が指定する。
    """
    from services.browser_pool import run_browser_page_task
    from services.html_page_adapter import HtmlPageAdapter
    from services.scrape_request import classify_target_url
    from services.scrape_safety import (
        install_navigation_guard,
        raise_for_blocked_navigation,
        validate_fetch_response,
        validate_marketplace_url,
    )

    request_kind, site = classify_target_url(url)
    kind = "detail" if request_kind == "item" else "search"
    normalized_url = validate_marketplace_url(url, site, kind=kind)
    timeout = max(1, int(kwargs.pop("timeout", 30000) or 30000))
    wait_selector = str(kwargs.pop("wait_selector", "") or "")
    wait_ms = max(0, int(kwargs.pop("wait", 0) or 0))
    allowed_statuses = frozenset(kwargs.pop("allowed_statuses", None) or ())
    if kwargs:
        unsupported = ", ".join(sorted(kwargs))
        raise TypeError(f"Unsupported guarded dynamic fetch options: {unsupported}")

    async def _fetch_guarded():
        page_state: dict[str, object] = {}

        async def _task(page, context):
            blocked_urls = await install_navigation_guard(context, site, kind=kind)
            try:
                response = await page.goto(
                    normalized_url,
                    wait_until="domcontentloaded",
                    timeout=timeout,
                )
            except Exception:
                raise_for_blocked_navigation(blocked_urls, site)
                raise
            raise_for_blocked_navigation(blocked_urls, site)
            if network_idle:
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(timeout, 5000))
                except Exception:
                    pass
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=min(timeout, 5000))
                except Exception:
                    pass
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            raise_for_blocked_navigation(blocked_urls, site)
            final_url = str(getattr(page, "url", "") or normalized_url)
            validate_marketplace_url(final_url, site, kind=kind)
            page_state["html"] = await page.content()
            page_state["url"] = final_url
            page_state["status"] = getattr(response, "status", None) or 200

        await run_browser_page_task(
            site,
            _task,
            headless=headless,
            launch_args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
            ],
        )
        result = HtmlPageAdapter(
            str(page_state.get("html") or ""),
            url=str(page_state.get("url") or normalized_url),
            status=int(page_state.get("status") or 200),
        )
        validate_fetch_response(result, site, kind=kind, allowed_statuses=allowed_statuses)
        return result

    return run_coro_sync(_fetch_guarded())
