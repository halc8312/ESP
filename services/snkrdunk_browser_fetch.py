"""
SNKRDUNK browser-pool DOM fetch helpers.
"""
from __future__ import annotations

import os

from services.browser_pool import run_browser_page_task
from services.html_page_adapter import HtmlPageAdapter
from services.scraping_client import run_coro_sync


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


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def should_use_snkrdunk_browser_pool_dynamic() -> bool:
    return _env_flag("SNKRDUNK_USE_BROWSER_POOL_DYNAMIC", default=False)


async def fetch_snkrdunk_page_via_browser_pool_async(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "a[href*='/products/'], script#__NEXT_DATA__, title",
) -> HtmlPageAdapter:
    page_state: dict[str, object] = {}

    async def _task(page, context):
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
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

    return HtmlPageAdapter(
        str(page_state.get("html") or ""),
        url=str(page_state.get("url") or url),
        status=int(page_state.get("status") or 200),
    )


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
