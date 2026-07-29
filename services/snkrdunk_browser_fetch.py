"""
SNKRDUNK browser-pool DOM fetch helpers.
"""
from __future__ import annotations

from services.browser_pool import run_browser_page_task
from services.html_page_adapter import HtmlPageAdapter
from services.scraping_client import run_coro_sync
from services.scrape_request import classify_target_url
from services.scrape_safety import (
    install_navigation_guard,
    raise_for_blocked_navigation,
    validate_fetch_response,
    validate_marketplace_url,
)
from utils.env_helpers import env_flag as _env_flag


_SNKRDUNK_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
]

_SNKRDUNK_CONTEXT_OPTIONS = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "extra_http_headers": {
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    },
}

_SNKRDUNK_INIT_SCRIPTS = [
    """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """
]


def should_use_snkrdunk_browser_pool_dynamic() -> bool:
    return _env_flag("SNKRDUNK_USE_BROWSER_POOL_DYNAMIC", default=False)


async def fetch_snkrdunk_page_via_browser_pool_async(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "a[href*='/products/'], script#__NEXT_DATA__, title",
) -> HtmlPageAdapter:
    request_kind, site = classify_target_url(url)
    if site != "snkrdunk":
        raise ValueError("SNKRDUNK以外のURLは取得できません。")
    kind = "detail" if request_kind == "item" else "search"
    url = validate_marketplace_url(url, "snkrdunk", kind=kind)
    page_state: dict[str, object] = {}

    async def _task(page, context):
        blocked_urls = await install_navigation_guard(context, "snkrdunk", kind=kind)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            raise_for_blocked_navigation(blocked_urls, "snkrdunk")
            raise
        raise_for_blocked_navigation(blocked_urls, "snkrdunk")
        if network_idle:
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=5000)
            except Exception:
                pass

        raise_for_blocked_navigation(blocked_urls, "snkrdunk")
        page_state["html"] = await page.content()
        page_state["url"] = page.url
        page_state["status"] = getattr(response, "status", None) or 200

    await run_browser_page_task(
        "snkrdunk",
        _task,
        headless=True,
        launch_args=_SNKRDUNK_LAUNCH_ARGS,
        context_options=_SNKRDUNK_CONTEXT_OPTIONS,
        init_scripts=_SNKRDUNK_INIT_SCRIPTS,
    )

    result = HtmlPageAdapter(
        str(page_state.get("html") or ""),
        url=str(page_state.get("url") or url),
        status=int(page_state.get("status") or 200),
    )
    validate_fetch_response(result, "snkrdunk", kind=kind)
    return result


def fetch_snkrdunk_page_via_browser_pool_sync(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "a[href*='/products/'], script#__NEXT_DATA__, title",
) -> HtmlPageAdapter:
    return run_coro_sync(
        fetch_snkrdunk_page_via_browser_pool_async(
            url,
            network_idle=network_idle,
            wait_selector=wait_selector,
        )
    )
