import asyncio
import base64
import json
import os
import time

import pytest
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from services import mercari_browser_fetch as fetcher
from services.mercari_browser_fetch import (
    fetch_mercari_page_and_payloads_via_browser_pool_async,
    fetch_mercari_page_and_payloads_via_browser_pool_sync,
    fetch_mercari_page_via_browser_pool_sync,
)
from services.operation_deadline import deadline_scope


TARGET = "m987654321"
ITEM_URL = f"https://jp.mercari.com/item/{TARGET}"
API_URL = f"https://api.mercari.jp/items/get?id={TARGET}&include_item_attributes=true"
PAYLOAD = {"data": {"id": TARGET, "name": "Target Item", "price": 2500, "status": "on_sale"}}


class FakeResponse:
    status = 200

    def __init__(self, url):
        self.url = url

    async def all_headers(self):
        # These old APIs may hang forever in production. Never call them.
        raise AssertionError("unbounded all_headers capture must not be used")

    async def json(self):
        raise AssertionError("unbounded response.json capture must not be used")


class FakeExpectedResponse:
    def __init__(self, page, predicate, timeout):
        self.page = page
        self.predicate = predicate
        self.timeout = timeout
        self.event = asyncio.Event()

    async def __aenter__(self):
        self.page.expected = self
        self.page.expect_registrations += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            return False
        try:
            await asyncio.wait_for(self.event.wait(), 0.02)
        except TimeoutError:
            raise PlaywrightTimeoutError("target response did not arrive")
        return False


class FakeCDPSession:
    def __init__(self, page):
        self.page = page
        self.handlers = {}
        self.commands = []
        self.bodies = {}
        self.positions = {}
        self.detached = False
        self.hang_read = False
        self.closed_streams = []
        self.response_headers = [{"name": "content-type", "value": "application/json"}]

    def on(self, event, callback):
        self.handlers[event] = callback

    def remove_listener(self, event, callback):
        assert self.handlers[event] == callback
        del self.handlers[event]

    def emit(self, body, *, response_url=API_URL, request_id=None):
        request_id = request_id or f"request-{len(self.bodies)}"
        self.bodies[request_id] = body
        self.positions[request_id] = 0
        callback = self.handlers.get("Fetch.requestPaused")
        if callback:
            callback({
                "requestId": request_id,
                "request": {"url": response_url},
                "responseStatusCode": 200,
                "responseHeaders": self.response_headers,
            })

    async def send(self, method, params):
        self.commands.append((method, params))
        if method == "Fetch.takeResponseBodyAsStream":
            return {"stream": params["requestId"]}
        if method == "IO.read":
            if self.hang_read:
                await asyncio.Future()
            stream = params["handle"]
            start = self.positions[stream]
            body = self.bodies[stream]
            chunk = body[start:start + params["size"]]
            self.positions[stream] += len(chunk)
            return {"data": base64.b64encode(chunk).decode(), "base64Encoded": True,
                    "eof": self.positions[stream] >= len(body)}
        if method == "Fetch.fulfillRequest":
            if self.page.expected and self.page.expected.predicate(FakeResponse(API_URL)):
                self.page.expected.event.set()
        if method == "IO.close":
            self.closed_streams.append(params["handle"])
        return {}

    async def detach(self):
        self.detached = True


class FakeBrowserContext:
    def __init__(self, page, *, cdp_available=True):
        self.session = FakeCDPSession(page)
        self.cdp_available = cdp_available

    async def route(self, _pattern, _handler):
        pass

    async def new_cdp_session(self, page):
        if not self.cdp_available:
            raise RuntimeError("CDP unavailable")
        return self.session


class FakePage:
    def __init__(self, *, url=ITEM_URL, initial_payload=True, reload_payload=True):
        self.url = url
        self.initial_payload = initial_payload
        self.reload_payload = reload_payload
        self.context = FakeBrowserContext(self)
        self.expected = None
        self.expect_registrations = 0
        self.reload_calls = 0
        self.carousel_calls = 0
        self.goto_calls = []
        self.body = json.dumps(PAYLOAD).encode()
        self.extra_events = []
        self.html = "<html><head><title>Target Item</title></head><body><h1>Target Item</h1></body></html>"

    def expect_response(self, predicate, timeout):
        return FakeExpectedResponse(self, predicate, timeout)

    async def goto(self, url, wait_until, timeout):
        self.goto_calls.append((url, wait_until, timeout))
        if fetcher._extract_mercari_item_id_from_url(url):
            assert self.expected is not None, "response wait must be registered before navigation"
        for response_url, body in self.extra_events:
            self.context.session.emit(body, response_url=response_url)
        if self.initial_payload:
            self.context.session.emit(self.body)
        return FakeResponse(url)

    async def reload(self, wait_until, timeout):
        self.reload_calls += 1
        assert self.expected is not None
        if self.reload_payload:
            self.context.session.emit(self.body)
        return FakeResponse(self.url)

    async def wait_for_load_state(self, state, timeout):
        pass

    async def wait_for_selector(self, selector, timeout):
        pass

    async def query_selector_all(self, selector):
        self.carousel_calls += 1
        return []

    async def query_selector(self, selector):
        return None

    async def wait_for_timeout(self, timeout):
        pass

    async def content(self):
        return self.html


def use_fake_page(monkeypatch, page):
    async def fake_run_browser_page_task(site, factory, **kwargs):
        assert site == "mercari"
        await factory(page, page.context)
    monkeypatch.setattr(fetcher, "run_browser_page_task", fake_run_browser_page_task)


def test_pinned_page_contract_has_expect_response_and_no_invented_wait():
    assert callable(Page.expect_response)
    assert not hasattr(Page, "wait_for_response")


def test_fetch_builds_html_adapter_and_captures_target_with_real_api_contract(monkeypatch):
    page = FakePage()
    use_fake_page(monkeypatch, page)
    result, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(ITEM_URL)
    assert result.url == ITEM_URL
    assert result.css("h1")[0].text == "Target Item"
    assert payloads == [{"url": API_URL, "payload": PAYLOAD}]
    assert page.expect_registrations == 1
    assert page.carousel_calls == 1
    assert page.context.session.detached
    assert not page.context.session.handlers


@pytest.mark.parametrize("url, matches", [
    (API_URL, True),
    (f"https://api.mercari.jp/items/get?id={TARGET}0", False),
    (f"https://api.mercari.jp/v2/relateditems/list?id={TARGET}", False),
    (f"https://api.mercari.jp.evil.test/items/get?id={TARGET}", False),
    (f"https://notmercari.test/items/get?id={TARGET}", False),
    (f"https://api.mercari.jp/items/get?id={TARGET}&id=m111", False),
    (f"https://api.mercari.jp:444/items/get?id={TARGET}", False),
])
def test_target_response_identity_exact_host_path_and_item(url, matches):
    assert fetcher._is_target_items_get(url, TARGET) is matches


def test_detail_reloads_sparse_first_navigation_once(monkeypatch):
    page = FakePage(initial_payload=False)
    use_fake_page(monkeypatch, page)
    _, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(ITEM_URL)
    assert page.reload_calls == 1
    assert page.expect_registrations == 2
    assert payloads[0]["payload"] == PAYLOAD


def test_patrol_skips_carousel_and_photo_only_reload(monkeypatch):
    page = FakePage(initial_payload=False)
    use_fake_page(monkeypatch, page)
    result, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(
        ITEM_URL, hydrate_images=False,
    )
    assert result.css("h1")
    assert payloads == []
    assert page.reload_calls == 0
    assert page.carousel_calls == 0


def test_non_item_skips_capture_wait_and_reload(monkeypatch):
    page = FakePage(url="https://jp.mercari.com/shop/abc", initial_payload=False)
    use_fake_page(monkeypatch, page)
    fetch_mercari_page_via_browser_pool_sync(page.url)
    assert page.expect_registrations == 0
    assert page.reload_calls == 0
    assert page.context.session.commands == []


def test_unsupported_cdp_does_not_fall_back_to_unbounded_response_json(monkeypatch):
    page = FakePage()
    page.context.cdp_available = False
    use_fake_page(monkeypatch, page)
    result, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(ITEM_URL)
    assert result.css("h1")
    assert payloads == []
    assert page.reload_calls == 0


def test_only_target_body_read_even_if_other_item_or_related_payload_is_large(monkeypatch):
    page = FakePage()
    page.extra_events = [
        ("https://api.mercari.jp/items/get?id=m111", b"x" * 10000),
        (f"https://api.mercari.jp/v2/relateditems/list?id={TARGET}", b"x" * 10000),
    ]
    use_fake_page(monkeypatch, page)
    _, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(ITEM_URL)
    assert payloads[0]["payload"] == PAYLOAD
    stream_reads = [params for method, params in page.context.session.commands
                    if method == "Fetch.takeResponseBodyAsStream"]
    assert stream_reads == [{"requestId": "request-2"}]


def test_oversized_body_aborted_before_full_materialization_without_content_length(monkeypatch):
    page = FakePage()
    page.body = b"x" * (20 * 1024 * 1024)
    use_fake_page(monkeypatch, page)
    _, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(ITEM_URL, hydrate_images=False)
    session = page.context.session
    assert payloads == []
    assert session.positions["request-0"] == fetcher._MAX_PAYLOAD_BYTES + 1
    assert all(params["size"] <= fetcher._STREAM_CHUNK_BYTES for method, params in session.commands if method == "IO.read")
    assert any(method == "Fetch.failRequest" for method, _ in session.commands)
    assert not any(method == "Fetch.fulfillRequest" for method, _ in session.commands)
    assert session.closed_streams == ["request-0"]


def test_misleading_content_length_does_not_bypass_stream_byte_limit(monkeypatch):
    page = FakePage()
    page.body = b"x" * (fetcher._MAX_PAYLOAD_BYTES + 2)
    page.context.session.response_headers.append({"name": "content-length", "value": "10"})
    use_fake_page(monkeypatch, page)
    _, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(ITEM_URL, hydrate_images=False)
    assert payloads == []
    assert page.context.session.positions["request-0"] == fetcher._MAX_PAYLOAD_BYTES + 1


@pytest.mark.asyncio
async def test_response_storm_has_bounded_queue_and_no_per_response_tasks(monkeypatch):
    page = FakePage(initial_payload=False)
    page.extra_events = [(API_URL, b"{}") for _ in range(10000)]
    use_fake_page(monkeypatch, page)
    before = set(asyncio.all_tasks())
    with pytest.raises(fetcher.MercariCaptureLimitExceeded, match="queue"):
        await fetch_mercari_page_and_payloads_via_browser_pool_async(ITEM_URL, hydrate_images=False)
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) == before
    assert not page.context.session.handlers
    assert page.context.session.detached


@pytest.mark.asyncio
async def test_never_returning_stream_read_obeys_deadline_and_cleans_up(monkeypatch):
    page = FakePage()
    page.context.session.hang_read = True
    use_fake_page(monkeypatch, page)
    started = time.monotonic()
    before = set(asyncio.all_tasks())
    with deadline_scope(started + 0.1):
        with pytest.raises(TimeoutError):
            await fetch_mercari_page_and_payloads_via_browser_pool_async(ITEM_URL, hydrate_images=False)
    assert time.monotonic() - started < 1.0
    assert page.context.session.closed_streams == ["request-0"]
    assert any(method == "Fetch.failRequest" for method, _ in page.context.session.commands)
    assert page.context.session.detached
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) == before


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("ESP_RUN_CHROMIUM_CAPTURE_INTEGRATION") != "1",
                    reason="set ESP_RUN_CHROMIUM_CAPTURE_INTEGRATION=1 for the pinned Chromium gate")
async def test_real_chromium_bounded_stream_preserves_spa_json_with_route_guard(monkeypatch):
    """Loopback-only gate for CDP semantics, including gzip entity decoding.

    Run with the pinned Playwright browser installed. This test intentionally
    skips only when the executable is absent, not on launch/interception errors.
    It never contacts a marketplace or database.
    """
    import gzip
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import threading

    from playwright.async_api import async_playwright

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/items/get"):
                oversized = "large=1" in self.path
                body = b"x" * (20 * 1024 * 1024) if oversized else json.dumps(PAYLOAD).encode()
                encoded = gzip.compress(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>fixture</h1></body></html>")

        def log_message(self, *args):
            pass

    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("pinned Playwright Chromium executable is not installed")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(fetcher, "_TARGET_API_PATTERN", f"{origin}/items/get?*")
        monkeypatch.setattr(fetcher, "_is_target_items_get", lambda url, item_id: url.startswith(f"{origin}/items/get?id={item_id}"))
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context()
            # Same request-stage route mechanism used by the navigation guard.
            await context.route("**/*", lambda route: route.continue_())
            page = await context.new_page()
            await page.goto(origin)
            capture = fetcher._BoundedPayloadCapture(TARGET, asyncio.current_task())
            try:
                await capture.start(page, context)
                payload = await asyncio.wait_for(page.evaluate(
                    "async url => (await fetch(url)).json()", f"{origin}/items/get?id={TARGET}"
                ), timeout=10)
                await capture.drain()
                assert payload == PAYLOAD
                assert capture.payloads[0]["payload"] == PAYLOAD
                assert capture.total_bytes == len(json.dumps(PAYLOAD).encode())
                # Compressed size is tiny; decompressed stream must still hit
                # the hard byte limit and abort before SPA can receive 20MiB.
                oversized_result = await asyncio.wait_for(page.evaluate(
                    "async url => { try { await (await fetch(url)).text(); return 'loaded'; } catch { return 'aborted'; } }",
                    f"{origin}/items/get?id={TARGET}&large=1",
                ), timeout=10)
                await capture.drain()
                assert oversized_result == "aborted"
                assert len(capture.payloads) == 1
            finally:
                await capture.close()
                await context.close()
        finally:
            await browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_total_stream_budget_applies_across_responses_and_discarded_bytes(monkeypatch):
    page = FakePage(initial_payload=False)
    large_json = json.dumps({"blob": "x" * 750000}).encode()
    page.extra_events = [(API_URL, large_json) for _ in range(4)]
    use_fake_page(monkeypatch, page)
    _, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(ITEM_URL, hydrate_images=False)
    assert len(payloads) == 2
    # Each rejected response may read one byte to identify overflow; all other
    # bytes, including partial/discarded bodies, count toward the common budget.
    assert sum(page.context.session.positions.values()) <= fetcher._MAX_TOTAL_PAYLOAD_BYTES + 2


@pytest.mark.asyncio
async def test_invalid_json_responses_still_consume_the_response_count_budget():
    page = FakePage()
    capture = fetcher._BoundedPayloadCapture(TARGET, asyncio.current_task())
    await capture.start(page, page.context)
    session = page.context.session
    try:
        for number in range(fetcher._MAX_CAPTURED_RESPONSES + 1):
            request_id = str(number)
            session.bodies[request_id] = b"invalid JSON"
            session.positions[request_id] = 0
            await capture._capture({
                "requestId": request_id, "request": {"url": API_URL},
                "responseStatusCode": 200, "responseHeaders": session.response_headers,
            })
        assert capture.payloads == []
        assert capture.captured_count == fetcher._MAX_CAPTURED_RESPONSES
        assert session.positions[str(fetcher._MAX_CAPTURED_RESPONSES)] == 0
        assert any(method == "Fetch.failRequest" for method, _ in session.commands)
    finally:
        await capture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_trigger", ["caller_cancel", "deadline"])
async def test_double_cancellation_does_not_hide_uncooperative_command(monkeypatch, initial_trigger):
    from services.browser_runtime import BrowserCleanupUnverified

    monkeypatch.setattr(fetcher, "_CANCEL_SETTLE_SECONDS", 0.08)
    started = asyncio.Event()
    settling = asyncio.Event()
    release = asyncio.Event()
    before = set(asyncio.all_tasks())

    async def uncooperative_rpc():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            settling.set()
            await release.wait()
        return "late RPC result"

    outer = asyncio.create_task(fetcher._await_with_limit(
        uncooperative_rpc(), 0.01 if initial_trigger == "deadline" else 5.0,
    ))
    await started.wait()
    if initial_trigger == "caller_cancel":
        outer.cancel()
    await settling.wait()
    # Cancel again while the initial cancellation/timeout is settling. The
    # uncooperative RPC remains active, so a plain CancelledError is unsafe.
    outer.cancel()
    await asyncio.sleep(0)
    outer.cancel()
    try:
        with pytest.raises(BrowserCleanupUnverified):
            await outer
        assert not release.is_set()
        assert len(set(asyncio.all_tasks()) - before) == 1
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) == before
