"""
Mercari browser-pool DOM fetch helpers.
"""
from __future__ import annotations

import asyncio
import os

from services.browser_pool import run_browser_page_task
from services.html_page_adapter import HtmlPageAdapter
from services.scraping_client import run_coro_sync


_MERCARI_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
]

_MERCARI_CONTEXT_OPTIONS = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "extra_http_headers": {
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    },
}

_MERCARI_INIT_SCRIPTS = [
    """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """
]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def should_use_mercari_browser_pool_detail() -> bool:
    return _env_flag("MERCARI_USE_BROWSER_POOL_DETAIL", default=False)


def should_use_mercari_browser_pool_patrol() -> bool:
    return _env_flag("MERCARI_PATROL_USE_BROWSER_POOL", default=False)


async def fetch_mercari_page_and_payloads_via_browser_pool_async(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "h1, [data-testid='price']",
) -> tuple[HtmlPageAdapter, list[dict]]:
    page_state: dict[str, object] = {}
    captured_payloads: list[dict] = []
    response_tasks: list[asyncio.Task] = []

    async def _task(page, context):
        async def _capture_response(response) -> None:
            try:
                headers = await response.all_headers()
            except Exception:
                headers = {}

            response_url = str(getattr(response, "url", "") or "")
            content_type = str(headers.get("content-type", "") or "").lower()
            if "mercari" not in response_url.lower():
                return
            if "json" not in content_type and not response_url.lower().endswith(".json"):
                return

            try:
                payload = await response.json()
            except Exception:
                return

            captured_payloads.append({"url": response_url, "payload": payload})

        page.on(
            "response",
            lambda response: response_tasks.append(asyncio.create_task(_capture_response(response))),
        )

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

        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)

        page_state["html"] = await page.content()
        page_state["url"] = page.url
        page_state["status"] = getattr(response, "status", None) or 200

    await run_browser_page_task(
        "mercari",
        _task,
        headless=True,
        launch_args=_MERCARI_LAUNCH_ARGS,
        context_options=_MERCARI_CONTEXT_OPTIONS,
        init_scripts=_MERCARI_INIT_SCRIPTS,
    )

    page = HtmlPageAdapter(
        str(page_state.get("html") or ""),
        url=str(page_state.get("url") or url),
        status=int(page_state.get("status") or 200),
    )
    return page, captured_payloads


def fetch_mercari_page_and_payloads_via_browser_pool_sync(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "h1, [data-testid='price']",
) -> tuple[HtmlPageAdapter, list[dict]]:
    return run_coro_sync(
        fetch_mercari_page_and_payloads_via_browser_pool_async(
            url,
            network_idle=network_idle,
            wait_selector=wait_selector,
        )
    )


async def fetch_mercari_page_via_browser_pool_async(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "h1, [data-testid='price']",
) -> HtmlPageAdapter:
    page, _ = await fetch_mercari_page_and_payloads_via_browser_pool_async(
        url,
        network_idle=network_idle,
        wait_selector=wait_selector,
    )
    return page


def fetch_mercari_page_via_browser_pool_sync(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "h1, [data-testid='price']",
) -> HtmlPageAdapter:
    return run_coro_sync(
        fetch_mercari_page_via_browser_pool_async(
            url,
            network_idle=network_idle,
            wait_selector=wait_selector,
        )
    )
