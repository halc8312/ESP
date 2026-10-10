"""
Mercari browser-pool DOM fetch helpers.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from services.browser_pool import run_browser_page_task
from services.browser_runtime import BrowserCleanupUnverified, bounded_browser_cleanup
from services.html_page_adapter import HtmlPageAdapter
from services.operation_deadline import current_deadline, deadline_scope, remaining_seconds
from services.scrape_request import classify_target_url
from services.scraping_client import run_coro_sync
from services.scrape_safety import (
    install_navigation_guard,
    raise_for_blocked_navigation,
    validate_fetch_response,
    validate_marketplace_url,
)
from utils.env_helpers import env_flag as _env_flag

_MERCARI_ITEM_ID_IN_URL = re.compile(r"/item/(m\d+)", re.IGNORECASE)
logger = logging.getLogger(__name__)

# Enforce bytes before JSON decoding, not by measuring an already-materialized
# Playwright Response.json()/body(). CDP's response-stage stream pauses before
# the body is delivered to the renderer; IO.read(size=...) bounds every read.
_MAX_PAYLOAD_BYTES = 1024 * 1024
_MAX_TOTAL_PAYLOAD_BYTES = 2 * 1024 * 1024
_MAX_PENDING_RESPONSES = 8
_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_CAPTURED_RESPONSES = 8
_TARGET_API_PATTERN = "https://api.mercari.jp/items/get?*"
_FETCH_DEADLINE_SECONDS = 120.0
_COMMAND_TIMEOUT_SECONDS = 5.0
_CANCEL_SETTLE_SECONDS = 0.25


class MercariCaptureLimitExceeded(RuntimeError):
    """A target response exceeded the bounded capture admission/buffer limit."""


def _is_target_items_get(response_url: str, target_item_id: str) -> bool:
    if not target_item_id:
        return False
    try:
        parsed = urlparse(response_url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.mercari.jp"
            and parsed.port in {None, 443}
            and parsed.path == "/items/get"
            and parse_qs(parsed.query).get("id") == [target_item_id]
        )
    except (TypeError, ValueError):
        return False


async def _await_with_limit(awaitable, seconds: float, *, cleanup: bool = False):
    """Bound a command, including cancellation verification.

    wait_for waits forever when its child ignores cancellation. Here an
    uncooperative child is reported to the owning browser runtime instead.
    """
    task = asyncio.ensure_future(awaitable)
    timeout = seconds if cleanup else remaining_seconds(seconds)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout or 0.0))
        if done:
            return task.result()
    except BaseException:
        await _cancel_command_and_verify(task)
        raise
    await _cancel_command_and_verify(task)
    raise TimeoutError("Mercari command exceeded the operation deadline")


async def _cancel_command_and_verify(task) -> None:
    # Install retrieval before any cancellable await. Repeated caller cancel
    # must not make a still-running RPC appear to have finished its cleanup.
    task.add_done_callback(_consume_task_result)
    task.cancel()

    async def settle() -> None:
        _, pending = await asyncio.wait({task}, timeout=_CANCEL_SETTLE_SECONDS)
        if pending:
            raise BrowserCleanupUnverified("Mercari command ignored cancellation")

    await bounded_browser_cleanup(
        settle(), label="Mercari command cancellation settlement",
        timeout=_CANCEL_SETTLE_SECONDS + 0.05,
    )


def _consume_task_result(task) -> None:
    if not task.cancelled():
        task.exception()


class _BoundedPayloadCapture:
    """One worker and a bounded queue, never one Task per page response.

    Only Chromium CDP supports this bounded stream. Other engines retain the
    DOM fallback; they deliberately do not fall back to unbounded body/json.
    Protocol contract: ChromeDevTools/devtools-protocol pdl/domains/Fetch.pdl
    (Response stage, takeResponseBodyAsStream) and IO.pdl (read's maximum size).
    """

    def __init__(self, target_item_id: str, owner_task):
        self.target_item_id = target_item_id
        self.owner_task = owner_task
        self.session = None
        self.worker = None
        self.queue = asyncio.Queue(maxsize=_MAX_PENDING_RESPONSES - 1)
        self.payloads: list[dict] = []
        self.total_bytes = 0
        self.captured_count = 0
        self.failure: BaseException | None = None
        self.accepting = False

    async def start(self, page, context) -> None:
        if not self.target_item_id:
            return
        # CDP is Chromium-only. Unsupported engines can still parse the DOM.
        try:
            self.session = await _await_with_limit(
                context.new_cdp_session(page), _COMMAND_TIMEOUT_SECONDS
            )
        except BrowserCleanupUnverified:
            raise
        except Exception:
            logger.warning("Mercari bounded payload capture unavailable; using DOM")
            return
        self.session.on("Fetch.requestPaused", self._on_paused)
        self.accepting = True
        self.worker = asyncio.create_task(self._run(), name="mercari-payload-stream")
        # Filter host/path in Chromium before events reach Python. The exact
        # query identity is checked synchronously before capture admission.
        await self._send("Fetch.enable", {"patterns": [{
            "urlPattern": _TARGET_API_PATTERN,
            "requestStage": "Response",
        }]})

    async def _send(self, method: str, params=None, *, cleanup=False):
        return await _await_with_limit(
            self.session.send(method, params or {}),
            1.0 if cleanup else _COMMAND_TIMEOUT_SECONDS,
            cleanup=cleanup,
        )

    def _on_paused(self, event: dict) -> None:
        if not self.accepting:
            return
        response_url = str((event.get("request") or {}).get("url") or "")
        matches = _is_target_items_get(response_url, self.target_item_id)
        # Non-target requests need a continue acknowledgement, but never get
        # response/body tasks. This fixed queue also bounds acknowledgements.
        try:
            self.queue.put_nowait((event, matches))
        except asyncio.QueueFull:
            self.failure = MercariCaptureLimitExceeded("Mercari response queue limit exceeded")
            self.accepting = False
            self.owner_task.cancel()

    async def _run(self) -> None:
        try:
            while True:
                event, matches = await self.queue.get()
                try:
                    if matches:
                        await self._capture(event)
                    else:
                        await self._send("Fetch.continueRequest", {"requestId": event["requestId"]})
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.failure = exc
            self.accepting = False
            self.owner_task.cancel()

    async def _capture(self, event: dict) -> None:
        request_id = event["requestId"]
        status = int(event.get("responseStatusCode") or 0)
        headers = event.get("responseHeaders") or []
        content_type = next((str(h.get("value") or "").lower() for h in headers
                             if str(h.get("name") or "").lower() == "content-type"), "")
        if status != 200 or "json" not in content_type:
            await self._send("Fetch.continueRequest", {"requestId": request_id})
            return
        stream = None
        resolved = False
        try:
            if self.captured_count >= _MAX_CAPTURED_RESPONSES:
                raise MercariCaptureLimitExceeded("Mercari payload count limit exceeded")
            self.captured_count += 1
            stream = (await self._send("Fetch.takeResponseBodyAsStream", {
                "requestId": request_id,
            }))["stream"]
            body = bytearray()
            while True:
                remaining = min(_MAX_PAYLOAD_BYTES - len(body),
                                _MAX_TOTAL_PAYLOAD_BYTES - self.total_bytes)
                # One extra byte distinguishes exact-limit EOF from overflow;
                # at most one bounded chunk exists outside the retained body.
                result = await self._send("IO.read", {
                    "handle": stream, "size": min(_STREAM_CHUNK_BYTES, remaining + 1),
                })
                data = result.get("data") or ""
                chunk = base64.b64decode(data, validate=True) if result.get("base64Encoded") else data.encode("utf-8")
                # Charge discarded partial responses as well. Otherwise a
                # rejected last chunk would let later responses reuse bytes.
                self.total_bytes += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    raise MercariCaptureLimitExceeded("Mercari payload byte limit exceeded")
                body.extend(chunk)
                if result.get("eof"):
                    break
            # Stream data are decoded entity bytes. Re-serving them must not
            # retain transport compression/length headers from the original.
            response_headers = [h for h in headers if str(h.get("name") or "").lower()
                                not in {"content-encoding", "content-length", "transfer-encoding"}]
            response_headers.append({"name": "content-length", "value": str(len(body))})
            await self._send("Fetch.fulfillRequest", {
                "requestId": request_id, "responseCode": status,
                "responseHeaders": response_headers,
                "body": base64.b64encode(body).decode("ascii"),
            })
            resolved = True
            try:
                payload = json.loads(body)
            except (ValueError, RecursionError):
                return
            if isinstance(payload, dict):
                self.payloads.append({"url": event["request"]["url"], "payload": payload})
        except MercariCaptureLimitExceeded:
            # Fail closed rather than deliver an oversized body to the SPA.
            # DOM parsing may still succeed; it never invents sold/deleted.
            logger.warning("Mercari target response exceeded capture budget")
        finally:
            async def finish_request():
                try:
                    if not resolved:
                        await self._send("Fetch.failRequest", {
                            "requestId": request_id, "errorReason": "Aborted",
                        }, cleanup=True)
                finally:
                    if stream is not None:
                        await self._send("IO.close", {"handle": stream}, cleanup=True)

            await bounded_browser_cleanup(finish_request(), label="Mercari intercepted stream teardown")

    async def drain(self) -> None:
        if self.worker:
            await _await_with_limit(self.queue.join(), _COMMAND_TIMEOUT_SECONDS)
        if self.failure is not None:
            raise self.failure

    async def close(self) -> None:
        self.accepting = False
        if self.session is None:
            return
        self.session.remove_listener("Fetch.requestPaused", self._on_paused)
        if self.worker:
            self.worker.cancel()
            done, pending = await asyncio.wait({self.worker}, timeout=3.0)
            if pending:
                self.worker.add_done_callback(_consume_task_result)
                raise BrowserCleanupUnverified("Mercari payload worker did not stop")
            for task in done:
                _consume_task_result(task)
        # Only disable/detach after the taken stream has been failed/closed.
        await self._send("Fetch.disable", cleanup=True)
        await _await_with_limit(self.session.detach(), 1.0, cleanup=True)


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
    hydrate_images: bool = True,
) -> tuple[HtmlPageAdapter, list[dict]]:
    request_kind, site = classify_target_url(url)
    if site != "mercari":
        raise ValueError("メルカリ以外のURLは取得できません。")
    kind = "detail" if request_kind == "item" else "search"
    url = validate_marketplace_url(url, "mercari", kind=kind)
    page_state: dict[str, object] = {}
    target_item_id = _extract_mercari_item_id_from_url(url)
    captured_payloads: list[dict] = []

    async def _best_effort_wait(awaitable, seconds):
        try:
            await _await_with_limit(awaitable, seconds)
        except BrowserCleanupUnverified:
            raise
        except TimeoutError:
            pass
        except Exception as exc:
            # Playwright's own timeout class is distinct from builtins.
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            if not isinstance(exc, PlaywrightTimeoutError):
                raise

    async def _task(page, context):
        capture = _BoundedPayloadCapture(target_item_id, asyncio.current_task())
        try:
            blocked_urls = await _await_with_limit(
                install_navigation_guard(context, "mercari", kind=kind), 5.0
            )
            await capture.start(page, context)

            async def navigate(*, reload=False):
                nonlocal response
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError

                action = page.reload if reload else page.goto
                action_args = () if reload else (url,)
                timeout_ms = max(1, int((remaining_seconds(30.0) or 0.0) * 1000))
                if target_item_id:
                    # Register BEFORE the action; expect_response is the actual
                    # pinned Playwright Python API. Never invent wait_for_response.
                    response_wait = page.expect_response(
                        lambda resp: _is_target_items_get(str(resp.url), target_item_id),
                        timeout=max(1, int((remaining_seconds(20.0) or 0.0) * 1000)),
                    )
                    navigated = False
                    try:
                        async with response_wait:
                            response = await _await_with_limit(
                                action(*action_args, wait_until="domcontentloaded", timeout=timeout_ms),
                                30.0,
                            )
                            navigated = True
                    except PlaywrightTimeoutError:
                        if not navigated:
                            raise
                        # Missing target XHR is a bounded best-effort wait;
                        # preserve the successfully navigated DOM fallback.
                else:
                    response = await _await_with_limit(
                        action(*action_args, wait_until="domcontentloaded", timeout=timeout_ms),
                        30.0,
                    )

            async def post_navigation_waits():
                if network_idle:
                    await _best_effort_wait(page.wait_for_load_state("networkidle", timeout=10000), 10.0)
                if wait_selector:
                    await _best_effort_wait(page.wait_for_selector(wait_selector, timeout=5000), 5.0)
                if hydrate_images:
                    await _await_with_limit(_click_through_image_carousel(page), 15.0)
                    if network_idle:
                        await _best_effort_wait(page.wait_for_load_state("networkidle", timeout=3000), 3.0)
                await capture.drain()

            response = None
            try:
                await _await_with_limit(navigate(), 30.0)
            except Exception:
                raise_for_blocked_navigation(blocked_urls, "mercari")
                raise
            raise_for_blocked_navigation(blocked_urls, "mercari")
            await post_navigation_waits()
            # Image recovery belongs to the full detail scraper. Patrol needs
            # price/status only and must not reload solely for missing photos.
            if hydrate_images and target_item_id and capture.session is not None and not capture.payloads:
                await _await_with_limit(navigate(reload=True), 30.0)
                raise_for_blocked_navigation(blocked_urls, "mercari")
                await post_navigation_waits()
            raise_for_blocked_navigation(blocked_urls, "mercari")
            page_state["html"] = await _await_with_limit(page.content(), 5.0)
            page_state["url"] = page.url
            page_state["status"] = getattr(response, "status", None) or 200
            captured_payloads.extend(capture.payloads)
        except asyncio.CancelledError:
            if capture.failure is not None:
                raise capture.failure
            raise
        finally:
            await bounded_browser_cleanup(capture.close(), label="Mercari response capture teardown")

    with deadline_scope(current_deadline() or (time.monotonic() + _FETCH_DEADLINE_SECONDS)):
        await run_browser_page_task(
            "mercari", _task, headless=True, launch_args=_MERCARI_LAUNCH_ARGS,
            context_options=_MERCARI_CONTEXT_OPTIONS, init_scripts=_MERCARI_INIT_SCRIPTS,
        )

    page = HtmlPageAdapter(
        str(page_state.get("html") or ""), url=str(page_state.get("url") or url),
        status=int(page_state.get("status") or 200),
    )
    validate_fetch_response(page, "mercari", kind=kind)
    return page, captured_payloads


def fetch_mercari_page_and_payloads_via_browser_pool_sync(
    url: str,
    *,
    network_idle: bool = True,
    wait_selector: str = "h1, [data-testid='price']",
    hydrate_images: bool = True,
) -> tuple[HtmlPageAdapter, list[dict]]:
    return run_coro_sync(
        fetch_mercari_page_and_payloads_via_browser_pool_async(
            url,
            network_idle=network_idle,
            wait_selector=wait_selector,
            hydrate_images=hydrate_images,
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
