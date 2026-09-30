"""Offline contracts for shared marketplace admission; never contact a live site."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import threading
from types import SimpleNamespace

import pytest

from services import marketplace_access as access
from services.scrape_safety import ScrapeBlockedError, ScrapeHttpError


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def run_async(coro):
    # Keep the selector waking while asyncio.Runner drains its executor. Some
    # sandboxed runtimes delay the thread-safe wakeup descriptor; this timer
    # leaves the governor and cancellation behavior unchanged.
    with asyncio.Runner() as runner:
        loop = runner.get_loop()

        def wakeup():
            loop.call_later(0.02, wakeup)

        wakeup()
        return runner.run(coro)


class ThreadedStore:
    """Model the blocking Redis boundary with an isolated memory algorithm."""
    def __init__(self, store, acquire):
        self._store = store
        self.acquire = acquire

    def __getattr__(self, name):
        return getattr(self._store, name)


def admit(store, owner, *, site="recordcity", parent=None, consume=True, interval=2, concurrency=1, lease_seconds=10):
    return store.acquire(site, owner, interval=interval, concurrency=concurrency,
                         lease_seconds=lease_seconds, consume=consume, parent=parent)


def test_memory_admission_is_atomic_across_competing_threads():
    store = access.MemoryAccessStore()
    barrier = threading.Barrier(12)

    def contender(index):
        barrier.wait(timeout=3)
        return admit(store, str(index), interval=0, concurrency=2)

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(contender, range(12)))
    assert sum(wait == 0 for wait, _ in results) == 2
    assert all(reason == "site_busy" for wait, reason in results if wait > 0)


def test_memory_interval_is_shared_after_owner_release_and_site_isolation():
    clock = Clock()
    store = access.MemoryAccessStore(clock)
    assert admit(store, "a") == (0, "")
    store.release("recordcity", "a")
    assert admit(store, "b") == (2, "site_interval")
    assert admit(store, "c", site="snkrdunk") == (0, "")
    clock.now += 2
    assert admit(store, "b") == (0, "")


def test_expired_owner_cannot_renew_reenter_or_release_successor():
    clock = Clock()
    store = access.MemoryAccessStore(clock)
    assert admit(store, "old", interval=0) == (0, "")
    clock.now += 10
    assert admit(store, "new", interval=0) == (0, "")
    assert store.renew("recordcity", "old", 10) is False
    with pytest.raises(access.AccessLeaseLost):
        admit(store, "old", parent="old", interval=0)
    store.release("recordcity", "old")
    assert admit(store, "third", interval=0)[1] == "site_busy"


def test_live_owner_renewal_prevents_early_replacement():
    clock = Clock()
    store = access.MemoryAccessStore(clock)
    admit(store, "owner", interval=0)
    clock.now += 9
    assert store.renew("recordcity", "owner", 10)
    clock.now += 2
    assert admit(store, "next", interval=0)[1] == "site_busy"
    clock.now += 8
    assert admit(store, "next", interval=0) == (0, "")


def test_nested_owner_respects_interval_and_pause_without_second_slot():
    clock = Clock()
    store = access.MemoryAccessStore(clock)
    assert admit(store, "owner", consume=False) == (0, "")
    assert admit(store, "owner", parent="owner") == (0, "")
    assert admit(store, "owner", parent="owner") == (2, "site_interval")
    store.pause("recordcity", 6)
    clock.now += 2
    store.pause("recordcity", 1)
    assert admit(store, "owner", parent="owner") == (4, "site_cooldown")
    clock.now += 4
    assert admit(store, "owner", parent="owner") == (0, "")


def test_released_owner_is_not_reentrant():
    store = access.MemoryAccessStore()
    admit(store, "owner", interval=0)
    store.release("recordcity", "owner")
    with pytest.raises(access.AccessLeaseLost):
        admit(store, "owner", parent="owner", interval=0)


@pytest.mark.parametrize("target,site", [
    ("recordcity", "recordcity"),
    ("https://www.recordcity.jp/ja/catalog?page=2", "recordcity"),
    ("https://snkrdunk.com/apparels/123456", "snkrdunk"),
    ("https://jp.mercari.com/item/m123456", "mercari"),
])
def test_site_keys_are_canonical_and_never_contain_product_paths(target, site):
    assert access.resolve_site(target) == site
    keys = access.RedisAccessStore("", client=object())._keys(access.resolve_site(target))
    assert all(f"{{{site}}}" in key for key in keys)
    assert all("/" not in key and "?" not in key for key in keys)


def test_unknown_site_has_no_body_side_effect():
    calls = []
    with pytest.raises(access.AccessBudgetExceeded):
        with access.marketplace_access("https://snkrdunk.com.attacker.test/apparels/1"):
            calls.append("request")
    assert calls == []


def test_production_without_redis_stops_before_network(monkeypatch):
    monkeypatch.setattr(access, "is_production_runtime", lambda: True)
    calls = []
    with pytest.raises(access.AccessStoreUnavailable):
        with access.marketplace_access("recordcity"):
            calls.append("request")
    assert calls == []
    assert access._store is None


def test_cached_development_store_is_not_reused_when_entering_production(monkeypatch):
    assert isinstance(access.get_access_store(), access.MemoryAccessStore)
    monkeypatch.setattr(access, "is_production_runtime", lambda: True)
    with pytest.raises(access.AccessStoreUnavailable):
        access.get_access_store()


class BrokenRedis:
    def eval(self, *args):
        raise ConnectionError("redis://credential-must-not-leak@private.example")

    def zrem(self, *args):
        raise ConnectionError("redis://credential-must-not-leak@private.example")


def test_redis_failure_never_falls_back_to_memory_or_exposes_credentials(monkeypatch):
    store = access.RedisAccessStore("", client=BrokenRedis())
    monkeypatch.setattr(access, "get_access_store", lambda: store)
    calls = []
    with pytest.raises(access.AccessStoreUnavailable) as error:
        with access.marketplace_access("recordcity"):
            calls.append("request")
    assert calls == []
    assert "credential" not in str(error.value)
    assert store.renew("recordcity", "owner", 10) is False
    with pytest.raises(access.AccessStoreUnavailable):
        store.pause("recordcity", 60)
    with pytest.raises(access.AccessStoreUnavailable):
        store.release("recordcity", "owner")


def test_sync_exception_releases_only_its_lease():
    store = access.get_access_store()
    with pytest.raises(ValueError, match="parser failed"):
        with access.marketplace_access("recordcity"):
            raise ValueError("parser failed")
    assert access.current_access_lease("recordcity") is None
    assert admit(store, "replacement", interval=0) == (0, "")


def test_explicit_wrong_or_stopped_parent_never_enters_request():
    store = access.get_access_store()
    wrong = access.AccessLease("snkrdunk", "owner", store)
    calls = []
    with pytest.raises(access.AccessLeaseLost):
        with access.marketplace_access("recordcity", parent_lease=wrong):
            calls.append("wrong")
    wrong.site = "recordcity"
    wrong.stop.set()
    with pytest.raises(access.AccessLeaseLost):
        with access.marketplace_access("recordcity", parent_lease=wrong):
            calls.append("stopped")
    assert calls == []


@pytest.mark.parametrize("header,expected", [("120", 120), ("2", 60), ("-1", 60), ("invalid", 60), ("999999", 3600)])
def test_429_retry_after_is_shared_and_bounded(monkeypatch, header, expected):
    clock = Clock()
    store = access.MemoryAccessStore(clock)
    monkeypatch.setattr(access, "get_access_store", lambda: store)
    with pytest.raises(ScrapeBlockedError) as error:
        access.observe_access_response("recordcity", 429, {"Retry-After": header})
    assert error.value.status_code == 429
    assert admit(store, "next", interval=0) == (expected, "site_cooldown")
    assert admit(store, "other", site="snkrdunk", interval=0) == (0, "")


def test_retry_after_http_date_uses_response_time(monkeypatch):
    clock = Clock(1_800_000_000)
    store = access.MemoryAccessStore(clock)
    monkeypatch.setattr(access, "get_access_store", lambda: store)
    monkeypatch.setattr(access, "time", SimpleNamespace(time=clock))
    value = format_datetime(datetime.fromtimestamp(clock.now + 180, timezone.utc), usegmt=True)
    with pytest.raises(ScrapeBlockedError):
        access.observe_access_response("recordcity", 429, {"retry-after": value})
    assert admit(store, "next", interval=0) == (180, "site_cooldown")


@pytest.mark.parametrize("status,headers,body,seconds,error_type", [
    (403, {}, "", 600, ScrapeBlockedError),
    (200, {"x-amzn-waf-action": "captcha"}, "", 600, ScrapeBlockedError),
    (200, {}, "Please verify you are human", 600, ScrapeBlockedError),
    (503, {}, "temporarily unavailable", 30, ScrapeHttpError),
])
def test_block_or_server_error_pauses_future_admission(monkeypatch, status, headers, body, seconds, error_type):
    store = access.MemoryAccessStore(Clock())
    monkeypatch.setattr(access, "get_access_store", lambda: store)
    with pytest.raises(error_type):
        access.observe_access_response("recordcity", status, headers, body)
    assert admit(store, "next", interval=0) == (seconds, "site_cooldown")


def test_normal_200_and_definitive_missing_responses_do_not_pause():
    for status in (200, 404, 410):
        access.observe_access_response("recordcity", status, body="A record album")
    assert admit(access.get_access_store(), "next", interval=0) == (0, "")


def test_wait_timeout_does_not_execute_body_or_leak_owner():
    store = access.get_access_store()
    store.pause("recordcity", 60)
    calls = []
    with pytest.raises(ScrapeBlockedError):
        with access.marketplace_access("recordcity", timeout_seconds=0):
            calls.append("request")
    assert calls == []
    assert not store.states["recordcity"]["leases"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_shared_retry_after_rejects_next_operation_immediately_without_sleep(monkeypatch, asynchronous):
    store = access.MemoryAccessStore(Clock())
    reports, calls = [], []
    monkeypatch.setattr(access, "get_access_store", lambda: store)
    monkeypatch.setattr(access, "_waiting", lambda reason, delay: reports.append((reason, delay)))

    def forbidden_sleep(*args):
        pytest.fail("Cooldown must not occupy a worker while sleeping")

    monkeypatch.setattr(access.time, "sleep", forbidden_sleep)
    monkeypatch.setattr(access.asyncio, "sleep", forbidden_sleep)
    with pytest.raises(ScrapeBlockedError):
        access.observe_access_response("recordcity", 429, {"Retry-After": "120"})

    async def operation():
        async with access.async_marketplace_access("recordcity"):
            calls.append("forbidden request")

    with pytest.raises(ScrapeBlockedError, match="120"):
        if asynchronous:
            run_async(operation())
        else:
            with access.marketplace_access("recordcity"):
                calls.append("forbidden request")
    assert reports == [("site_cooldown", 120)]
    assert calls == []
    assert not store.states["recordcity"]["leases"]


def test_nested_budget_is_shared_and_rejection_has_no_network_effect():
    calls = []
    with access.request_budget(max_requests=2) as outer:
        with access.request_budget(max_requests=100) as inner:
            assert inner is outer
            for _ in range(2):
                with access.marketplace_access("recordcity"):
                    calls.append("request")
            with pytest.raises(access.AccessBudgetExceeded):
                with access.marketplace_access("recordcity"):
                    calls.append("forbidden")
    assert calls == ["request", "request"]
    assert outer.requests == 2
    with access.request_budget(max_requests=1) as fresh:
        assert fresh.requests == 0
        with access.marketplace_access("recordcity"):
            pass


def test_elapsed_budget_stops_before_store_admission(monkeypatch):
    calls = []
    with access.request_budget(max_seconds=1) as budget:
        budget.started_at -= 2
        with pytest.raises(access.AccessBudgetExceeded):
            with access.marketplace_access("recordcity"):
                calls.append("request")
        budget.started_at += 2
    assert calls == []
    assert access._store is None


def test_async_nested_cancellation_preserves_parent_until_parent_exit():
    async def exercise():
        child_entered = asyncio.Event()

        async def child(parent):
            async with access.async_marketplace_access("recordcity", parent_lease=parent):
                child_entered.set()
                await asyncio.Event().wait()

        async with access.async_marketplace_access("recordcity", consume_request=False) as parent:
            task = asyncio.create_task(child(parent))
            await asyncio.wait_for(child_entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert access.current_access_lease("recordcity") is parent
            assert admit(parent.store, "competing", interval=0)[1] == "site_busy"
            async with access.async_marketplace_access("recordcity", parent_lease=parent) as still_parent:
                assert still_parent is parent
        assert access.current_access_lease("recordcity") is None
        assert admit(parent.store, "replacement", interval=0) == (0, "")

    run_async(exercise())


def test_async_top_level_cancellation_releases_owner():
    async def exercise():
        entered = asyncio.Event()

        async def operation():
            async with access.async_marketplace_access("recordcity"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(operation())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admit(access.get_access_store(), "replacement", interval=0) == (0, "")

    run_async(exercise())


def test_async_cancel_during_store_admission_releases_granted_owner(monkeypatch):
    entered = threading.Event()
    finish = threading.Event()
    store = access.MemoryAccessStore()
    original = store.acquire

    def delayed(*args, **kwargs):
        entered.set()
        assert finish.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(access, "get_access_store", lambda: ThreadedStore(store, delayed))

    async def exercise():
        calls = []

        async def operation():
            async with access.async_marketplace_access("recordcity"):
                calls.append("forbidden")

        task = asyncio.create_task(operation())
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0)
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            finish.set()
        assert calls == []
        assert original("recordcity", "replacement", interval=0, concurrency=1,
                        lease_seconds=10, consume=True) == (0, "")

    run_async(exercise())


def test_async_repeated_cancel_during_admission_does_not_leak_owner(monkeypatch):
    entered = threading.Event()
    finish = threading.Event()
    store = access.MemoryAccessStore()
    original = store.acquire

    def delayed(*args, **kwargs):
        entered.set()
        assert finish.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(access, "get_access_store", lambda: ThreadedStore(store, delayed))

    async def exercise():
        calls = []

        async def operation():
            async with access.async_marketplace_access("recordcity"):
                calls.append("forbidden")

        task = asyncio.create_task(operation())
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            finish.set()
        assert calls == []
        assert original("recordcity", "replacement", interval=0, concurrency=1,
                        lease_seconds=10, consume=True) == (0, "")

    run_async(exercise())


def test_inherited_context_does_not_let_parallel_children_share_parent_slot():
    async def exercise():
        calls = []

        async def child():
            with pytest.raises(ScrapeBlockedError):
                async with access.async_marketplace_access("recordcity", timeout_seconds=0):
                    calls.append("forbidden")

        async with access.async_marketplace_access("recordcity", consume_request=False):
            await asyncio.gather(child(), child())
        assert calls == []

    run_async(exercise())


def test_budget_propagates_to_async_and_to_thread_operations():
    calls = []

    def sync_request():
        with access.marketplace_access("recordcity"):
            calls.append("thread")

    async def exercise():
        with access.request_budget(max_requests=2) as budget:
            await asyncio.to_thread(sync_request)
            async with access.async_marketplace_access("recordcity"):
                calls.append("async")
            with pytest.raises(access.AccessBudgetExceeded):
                await asyncio.to_thread(sync_request)
            assert budget.requests == 2

    run_async(exercise())
    assert calls == ["thread", "async"]


def test_budget_is_preserved_through_sync_wrapper_inside_running_loop():
    from services.scraping_client import run_coro_sync
    calls = []

    async def request():
        async with access.async_marketplace_access("recordcity"):
            calls.append("request")

    async def exercise():
        with access.request_budget(max_requests=1) as budget:
            run_coro_sync(request())
            with pytest.raises(access.AccessBudgetExceeded):
                run_coro_sync(request())
            assert budget.requests == 1

    run_async(exercise())
    assert calls == ["request"]
