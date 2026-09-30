"""Admission at actual transport boundaries, without live marketplace traffic."""
import asyncio
import contextvars
from concurrent.futures import Future
import sys
from types import ModuleType, SimpleNamespace

import pytest

from services import marketplace_access as admission
from services import scraping_client
from services.scrape_safety import (
    ScrapeBlockedError, UnsafeScrapeUrlError, install_navigation_guard,
    raise_for_blocked_navigation,
)


def page(url, status=200, body="商品", headers=None):
    return SimpleNamespace(url=url, status=status, body=body, headers=headers or {})


def install_fetcher(monkeypatch, fetch):
    module = ModuleType("scrapling")
    module.Fetcher = SimpleNamespace(get=fetch)
    monkeypatch.setitem(sys.modules, "scrapling", module)


def immediate_admission(monkeypatch):
    original = admission.marketplace_access
    monkeypatch.setattr(
        admission, "marketplace_access",
        lambda site, **kwargs: original(site, timeout_seconds=0, **kwargs),
    )


def test_static_redirects_consume_separate_requests_before_dispatch(monkeypatch):
    target = "https://snkrdunk.com/products/ABC"
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        assert kwargs["follow_redirects"] is False
        assert kwargs["retries"] == 1
        return page(url, 302, headers={"Location": url + "?next=1"})

    install_fetcher(monkeypatch, fetch)
    with admission.request_budget(max_requests=2) as budget:
        with pytest.raises(admission.AccessBudgetExceeded):
            scraping_client.fetch_static(target)
    assert budget.requests == 2
    assert len(calls) == 2


def test_unsafe_static_target_and_redirect_never_reach_next_transport(monkeypatch):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return page(url, 302, headers={"Location": "http://127.0.0.1/private"})

    install_fetcher(monkeypatch, fetch)
    with pytest.raises((UnsafeScrapeUrlError, ValueError)):
        scraping_client.fetch_static("http://127.0.0.1/private")
    assert calls == []
    with pytest.raises(UnsafeScrapeUrlError):
        scraping_client.fetch_static("https://snkrdunk.com/products/ABC")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_retry_consumes_each_actual_attempt(monkeypatch):
    calls = []
    target = "https://item.fril.jp/ABC"

    async def fetch(url, **kwargs):
        calls.append(url)
        assert kwargs["retries"] == 1
        if len(calls) == 1:
            raise RuntimeError("temporary connection failure")
        return page(url)

    module = ModuleType("scrapling.fetchers")
    module.AsyncFetcher = SimpleNamespace(get=fetch)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", module)
    with admission.request_budget(max_requests=2) as budget:
        result = await scraping_client.fetch_static_async(target, retries=1)
    assert result.url == target
    assert calls == [target, target]
    assert budget.requests == 2


@pytest.mark.asyncio
async def test_429_does_not_retry_or_switch_transport(monkeypatch):
    target = "https://snkrdunk.com/products/ABC"
    calls = []

    async def fetch(url, **kwargs):
        calls.append("async")
        return page(url, 429, headers={"Retry-After": "120"})

    module = ModuleType("scrapling.fetchers")
    module.AsyncFetcher = SimpleNamespace(get=fetch)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", module)
    install_fetcher(monkeypatch, lambda *args, **kwargs: calls.append("sync"))
    with pytest.raises(ScrapeBlockedError):
        await scraping_client.fetch_static_async(target, retries=3)
    immediate_admission(monkeypatch)
    with pytest.raises(ScrapeBlockedError):
        scraping_client.fetch_static(target)
    assert calls == ["async"]


@pytest.mark.asyncio
async def test_browser_lease_lasts_until_failed_task_stops_and_then_releases(monkeypatch):
    from services import browser_pool

    monkeypatch.setattr(browser_pool, "get_browser_runtime", lambda *args, **kwargs: None)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def temporary(task, **kwargs):
        return await task(SimpleNamespace(), SimpleNamespace())

    monkeypatch.setattr(browser_pool, "_run_with_temporary_browser", temporary)

    async def task(page, context):
        started.set()
        await finish.wait()
        raise RuntimeError("browser task failed")

    running = asyncio.create_task(browser_pool.run_browser_page_task("mercari", task))
    await started.wait()
    # The independent caller has no task-local parent and must not overlap.
    with pytest.raises(ScrapeBlockedError):
        with admission.marketplace_access("mercari", timeout_seconds=0):
            pytest.fail("another caller entered while the browser was active")
    finish.set()
    with pytest.raises(RuntimeError, match="browser task failed"):
        await running
    with admission.marketplace_access("mercari", timeout_seconds=0):
        pass


@pytest.mark.asyncio
async def test_navigation_callback_reuses_captured_lease_in_separate_context():
    class Context:
        async def route(self, pattern, handler):
            self.handler = handler

    class Route:
        continued = False
        aborted = False

        async def continue_(self):
            self.continued = True

        async def abort(self, reason):
            self.aborted = True

    request = SimpleNamespace(
        url="https://recordcity.jp/ja/catalog/123", resource_type="document",
        is_navigation_request=lambda: True, frame=None,
    )
    with admission.request_budget(max_requests=1) as budget:
        async with admission.async_marketplace_access("recordcity", consume_request=False):
            context = Context()
            failures = await install_navigation_guard(context, "recordcity", kind="detail")
            route = Route()
            callback = contextvars.Context().run(asyncio.create_task, context.handler(route, request))
            await callback
            raise_for_blocked_navigation(failures, "recordcity")
            assert route.continued and not route.aborted
    # The callback's empty ContextVars must retain the original job's budget
    # through the captured lease, rather than silently losing request limits.
    assert budget.requests == 1


@pytest.mark.asyncio
async def test_shared_browser_cancellation_keeps_lease_through_context_cleanup(monkeypatch):
    from services import browser_pool

    begun = asyncio.Event()
    closing = asyncio.Event()
    allow_close = asyncio.Event()

    class Context:
        async def new_page(self):
            return SimpleNamespace()

        async def close(self):
            closing.set()
            await allow_close.wait()

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

    class Runtime:
        def submit(self, factory):
            bridge = Future()
            task = asyncio.create_task(factory(Browser()))

            def cancel_task(future):
                if future.cancelled():
                    task.cancel()

            def finish(completed):
                if not bridge.cancelled():
                    if completed.cancelled():
                        bridge.cancel()
                    elif completed.exception() is not None:
                        bridge.set_exception(completed.exception())
                    else:
                        bridge.set_result(completed.result())

            bridge.add_done_callback(cancel_task)
            task.add_done_callback(finish)
            return bridge

    monkeypatch.setattr(browser_pool, "get_browser_runtime", lambda *args, **kwargs: Runtime())

    async def task(page, context):
        begun.set()
        await asyncio.Event().wait()

    running = asyncio.create_task(browser_pool.run_browser_page_task("mercari", task))
    await begun.wait()
    running.cancel()
    await closing.wait()
    with pytest.raises(ScrapeBlockedError):
        with admission.marketplace_access("mercari", timeout_seconds=0):
            pytest.fail("cancelled browser released its lease before cleanup")
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    with admission.marketplace_access("mercari", timeout_seconds=0):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_site", ["recordcity_headful", "recordcity_persistent_chrome"])
async def test_recordcity_runtime_alias_owns_base_site_before_startup(monkeypatch, runtime_site):
    from services import browser_pool

    def lookup(site, **kwargs):
        assert admission.current_access_lease("recordcity") is not None
        assert site == runtime_site
        return None

    async def temporary(task, **kwargs):
        return await task(SimpleNamespace(), SimpleNamespace())

    monkeypatch.setattr(browser_pool, "get_browser_runtime", lookup)
    monkeypatch.setattr(browser_pool, "_run_with_temporary_browser", temporary)
    with admission.request_budget(max_requests=1) as budget:
        result = await browser_pool.run_browser_page_task(runtime_site, lambda *args: asyncio.sleep(0, result="ok"))
    assert result == "ok"
    assert budget.requests == 0, "starting a browser is not a marketplace request"


def test_external_provider_uses_target_site_shared_pause(monkeypatch):
    from curl_cffi import requests

    monkeypatch.setenv("SURUGAYA_SCRAPERAPI_KEY", "fixture-key")
    for name in ("SURUGAYA_ZYTE_API_KEY", "SURUGAYA_FETCH_API_URL_TEMPLATE", "SURUGAYA_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    monkeypatch.setattr(
        requests, "get",
        lambda *args, **kwargs: calls.append("provider") or SimpleNamespace(status_code=403, text="denied", headers={}),
    )
    with pytest.raises(ScrapeBlockedError):
        scraping_client.fetch_surugaya_external("https://www.suruga-ya.jp/product/detail/123")
    immediate_admission(monkeypatch)
    with pytest.raises(ScrapeBlockedError):
        with admission.marketplace_access("surugaya"):
            pytest.fail("provider failure did not pause Surugaya")
    assert calls == ["provider"]


def test_external_json_decode_does_not_consume_network_request_budget(monkeypatch):
    from curl_cffi import requests

    monkeypatch.setenv("SURUGAYA_ZYTE_API_KEY", "fixture-key")
    target = "https://www.suruga-ya.jp/product/detail/123"
    response = SimpleNamespace(
        status_code=200, headers={}, text="fixture JSON",
        json=lambda: {"browserHtml": "<h1>商品</h1>", "statusCode": 200},
    )
    calls = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append("provider") or response)
    with admission.request_budget(max_requests=1) as budget:
        result = scraping_client.fetch_surugaya_external(target)
    assert result.url == target
    assert calls == ["provider"]
    assert budget.requests == 1


def test_sync_coroutine_thread_preserves_request_budget():
    async def child():
        with admission.marketplace_access("rakuma"):
            pass

    async def caller():
        scraping_client.run_coro_sync(child())

    with admission.request_budget(max_requests=1) as budget:
        scraping_client.run_coro_sync(caller())
    assert budget.requests == 1
