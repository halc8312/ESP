"""
Mercari browser-pool DOM fetch helpers.
"""
from __future__ import annotations

import asyncio
import os
import re

from services.browser_pool import run_browser_page_task
from services.html_page_adapter import HtmlPageAdapter
from services.scraping_client import run_coro_sync

_MERCARI_ITEM_ID_IN_URL = re.compile(r"/item/(m\d+)", re.IGNORECASE)


def _extract_mercari_item_id_from_url(url: str) -> str:
    match = _MERCARI_ITEM_ID_IN_URL.search(url or "")
    return match.group(1).lower() if match else ""


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


_CAROUSEL_CLICK_TIMEOUT_MS = 1000
_MAX_CAROUSEL_NEXT_CLICKS = 10


async def _click_through_image_carousel(page) -> None:
    """Click through Mercari's image carousel to render all lazy-loaded images.

    Mercari's SPA only renders the first 1-2 images initially.  By clicking
    each thumbnail (``[data-testid^='imageThumbnail-']``) or pressing the
    carousel's next-arrow, we force the framework to mount the remaining
    ``<img>`` elements so that ``page.content()`` includes every image URL.
    """
    try:
        # Strategy 1: click each thumbnail to reveal every image
        thumbnails = await page.query_selector_all(
            "[data-testid^='imageThumbnail-']"
        )
        for thumb in thumbnails:
            try:
                await thumb.click(timeout=_CAROUSEL_CLICK_TIMEOUT_MS)
                await page.wait_for_timeout(300)
            except Exception:
                continue

        # Strategy 2: click the carousel next-button repeatedly
        # (covers pages whose thumbnails are off-screen)
        for _ in range(_MAX_CAROUSEL_NEXT_CLICKS):
            next_btn = await page.query_selector(
                "[data-testid='carousel'] button[aria-label*='次'], "
                "[data-testid='carousel'] button[aria-label*='next'], "
                "[data-testid='carousel'] button:last-child"
            )
            if not next_btn:
                break
            is_disabled = await next_btn.get_attribute("disabled")
            if is_disabled is not None:
                break
            try:
                await next_btn.click(timeout=_CAROUSEL_CLICK_TIMEOUT_MS)
                await page.wait_for_timeout(300)
            except Exception:
                break

        # Wait briefly for any lazy images triggered by the carousel clicks
        await page.wait_for_timeout(500)
    except Exception:
        pass


async def fetch_mercari_page_and_payloads_via_browser_pool_async(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "h1, [data-testid='price']",
) -> tuple[HtmlPageAdapter, list[dict]]:
    page_state: dict[str, object] = {}
    captured_payloads: list[dict] = []
    response_tasks: list[asyncio.Task] = []

    target_item_id = _extract_mercari_item_id_from_url(url)

    def _has_target_items_get_response() -> bool:
        """True when captured_payloads already contains the target item's
        ``/items/get?id=<TARGET>`` XHR response.  That response carries the
        canonical photo list, so its presence is our strongest signal that
        the browser successfully hydrated the detail page."""
        if not target_item_id:
            return True  # Non-item URLs (shop/catalog) never need this check.
        for cap in captured_payloads:
            if not isinstance(cap, dict):
                continue
            cap_url = str(cap.get("url") or "").lower()
            if "items/get" in cap_url and target_item_id in cap_url:
                return True
        return False

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

    async def _perform_post_navigation_waits(page) -> None:
        """Wait for Mercari's SPA to hydrate, fire ``/items/get?id=<TARGET>``,
        and expand the image carousel.  Used on both the initial navigation
        and the sparse-state reload retry below."""
        if target_item_id:
            def _matches_target_items_get(resp) -> bool:
                resp_url = str(getattr(resp, "url", "") or "").lower()
                return "items/get" in resp_url and target_item_id in resp_url

            try:
                await page.wait_for_response(
                    _matches_target_items_get, timeout=20000
                )
            except Exception:
                pass

        if network_idle:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=5000)
            except Exception:
                pass

        # Click through the image carousel to render all lazy-loaded images
        await _click_through_image_carousel(page)

        # Give any late-firing XHRs (thumbnails, related items, photo
        # details) a second networkidle pass so our response listener can
        # capture them.
        if network_idle:
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

    async def _drain_response_tasks() -> None:
        """Await every in-flight ``_capture_response`` task so that
        ``captured_payloads`` reflects the latest network state.  Called
        between the initial navigation and the sparse-state reload check
        so the reload decision is based on fully-drained data."""
        if not response_tasks:
            return
        pending = list(response_tasks)
        response_tasks.clear()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _task(page, context):
        page.on(
            "response",
            lambda response: response_tasks.append(asyncio.create_task(_capture_response(response))),
        )

        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # On production (Render US) the /items/get?id=<TARGET> XHR sometimes
        # lands *after* the default networkidle window, which caused the
        # detail scraper to fall back to just the og:image (one image).
        # ``_perform_post_navigation_waits`` explicitly waits (best effort)
        # for that response so the photo URL union post-pass always has a
        # target-scoped blob to union from.
        await _perform_post_navigation_waits(page)
        await _drain_response_tasks()

        # Some Mercari item pages hydrate so slowly in production that even
        # after the full wait window above, ``/items/get?id=<TARGET>`` has
        # not fired and the carousel has not mounted — leaving us with just
        # og:image and a single-photo result.  In that sparse state, doing
        # one in-browser ``page.reload()`` (warm cache, warm cookies) almost
        # always recovers the real photo set on the second attempt.  We
        # only retry once, only for item URLs, and only when we haven't
        # already seen the target's ``/items/get`` response.
        if target_item_id and not _has_target_items_get_response():
            try:
                reload_response = await page.reload(
                    wait_until="domcontentloaded", timeout=30000
                )
                if reload_response is not None:
                    response = reload_response
                await _perform_post_navigation_waits(page)
                await _drain_response_tasks()
            except Exception:
                pass

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
