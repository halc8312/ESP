"""Real Lua contracts against an explicitly selected, loopback-only CI Redis.

No production Redis URL is read. Every test owns a random key prefix and
deletes only those exact keys; FLUSHDB/FLUSHALL are never used.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
from urllib.parse import urlparse
import uuid

import pytest

from services import marketplace_access as access
from services.scrape_safety import ScrapeBlockedError


class IsolatedRedisAccessStore(access.RedisAccessStore):
    def __init__(self, url, prefix):
        super().__init__(url)
        self.prefix = prefix

    def _keys(self, site):
        return [self.prefix + key for key in super()._keys(site)]


@pytest.fixture
def redis_stores():
    url = os.environ.get("MARKETPLACE_ACCESS_TEST_REDIS_URL")
    if not url:
        pytest.skip("An isolated loopback Redis is required for Lua integration tests")
    parsed = urlparse(url)
    if parsed.scheme != "redis" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("MARKETPLACE_ACCESS_TEST_REDIS_URL must target loopback Redis only")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        pytest.fail("The isolated Redis test URL must not contain credentials or query options")
    prefix = f"esp:test:{uuid.uuid4().hex}:"
    stores = [IsolatedRedisAccessStore(url, prefix), IsolatedRedisAccessStore(url, prefix)]
    try:
        # An explicitly configured but unavailable CI service is a failure,
        # never a skipped gate or a fallback to the production connection.
        assert all(store.client.ping() for store in stores)
        yield stores
    finally:
        try:
            for site in ("recordcity", "snkrdunk"):
                stores[0].client.delete(*stores[0]._keys(site))
        finally:
            for store in stores:
                store.client.close()


def admit(store, owner, *, site="recordcity", interval=0, concurrency=1,
          lease_seconds=3, consume=True, parent=None):
    return store.acquire(site, owner, interval=interval, concurrency=concurrency,
                         lease_seconds=lease_seconds, consume=consume, parent=parent)


def test_lua_acquire_is_atomic_across_independent_clients(redis_stores):
    barrier = threading.Barrier(12)

    def attempt(index):
        barrier.wait(timeout=3)
        return str(index), admit(redis_stores[index % 2], str(index))

    with ThreadPoolExecutor(max_workers=12) as executor:
        attempts = list(executor.map(attempt, range(12)))
    winners = [owner for owner, (wait, _) in attempts if wait == 0]
    assert len(winners) == 1
    assert all(reason == "site_busy" for _, (wait, reason) in attempts if wait > 0)
    redis_stores[1].release("recordcity", winners[0])
    assert admit(redis_stores[0], "replacement") == (0, "")


def test_lua_global_concurrency_setting_two_is_an_upper_bound(redis_stores):
    assert admit(redis_stores[0], "one", concurrency=2) == (0, "")
    assert admit(redis_stores[1], "two", concurrency=2) == (0, "")
    assert admit(redis_stores[0], "three", concurrency=2)[1] == "site_busy"
    assert admit(redis_stores[1], "other-site", site="snkrdunk", concurrency=2) == (0, "")


def test_lua_expiry_rejects_parent_and_wrong_owner_cannot_release_successor(redis_stores):
    first, second = redis_stores
    assert admit(first, "expired", lease_seconds=0.12) == (0, "")
    time.sleep(0.15)
    assert admit(second, "successor") == (0, "")
    assert first.renew("recordcity", "expired", 3) is False
    with pytest.raises(access.AccessLeaseLost):
        admit(first, "expired", parent="expired")
    first.release("recordcity", "expired")
    assert admit(first, "third")[1] == "site_busy"
    assert second.client.ttl(second._keys("recordcity")[0]) > 0


def test_lua_renew_extends_live_lease_and_keeps_owner_identity(redis_stores):
    first, second = redis_stores
    assert admit(first, "owner", lease_seconds=0.4) == (0, "")
    assert second.renew("recordcity", "wrong", 3) is False
    assert first.renew("recordcity", "owner", 3) is True
    time.sleep(0.45)
    assert admit(second, "competitor")[1] == "site_busy"
    assert admit(second, "owner", parent="owner") == (0, "")


def test_lua_interval_survives_release_and_is_shared_between_clients(redis_stores):
    first, second = redis_stores
    assert admit(first, "one", interval=0.15) == (0, "")
    first.release("recordcity", "one")
    wait, reason = admit(second, "two", interval=0.15)
    assert reason == "site_interval"
    assert 0 < wait <= 0.15
    time.sleep(wait + 0.01)
    assert admit(second, "two", interval=0.15) == (0, "")


def test_lua_retry_after_pause_is_shared_and_cannot_be_shortened(redis_stores, monkeypatch):
    first, second = redis_stores
    monkeypatch.setattr(access, "get_access_store", lambda: first)
    with pytest.raises(ScrapeBlockedError):
        access.observe_access_response("recordcity", 429, {"Retry-After": "125"})
    second.pause("recordcity", 5)
    wait, reason = admit(second, "next")
    assert reason == "site_cooldown"
    assert 120 <= wait <= 125
    assert admit(second, "other", site="snkrdunk") == (0, "")
    assert 120 <= second.client.ttl(second._keys("recordcity")[2]) <= 185


def test_lua_browser_parent_bypasses_own_slot_but_not_shared_interval(redis_stores):
    first, second = redis_stores
    assert admit(first, "browser", consume=False) == (0, "")
    assert admit(second, "browser", parent="browser", interval=0.15) == (0, "")
    assert admit(first, "browser", parent="browser", interval=0.15)[1] == "site_interval"
    first.release("recordcity", "browser")
    with pytest.raises(access.AccessLeaseLost):
        admit(second, "browser", parent="browser")


def test_two_client_workers_never_overlap_network_bodies(redis_stores, monkeypatch):
    # Separate clients represent workers in different processes. Lua owns the
    # synchronization; the Python lock is used only to measure real overlap.
    local = threading.local()
    monkeypatch.setattr(access, "get_access_store", lambda: local.store)
    monkeypatch.setenv("RECORDCITY_ACCESS_INTERVAL_SECONDS", "0.03")
    monkeypatch.setenv("RECORDCITY_ACCESS_CONCURRENCY", "1")
    barrier = threading.Barrier(6)
    lock = threading.Lock()
    counters = {"active": 0, "peak": 0}
    starts = []

    def worker(index):
        local.store = redis_stores[index % 2]
        barrier.wait(timeout=3)
        with access.marketplace_access("recordcity", timeout_seconds=5):
            with lock:
                counters["active"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
                starts.append(time.monotonic())
            try:
                time.sleep(0.015)
            finally:
                with lock:
                    counters["active"] -= 1

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(worker, range(6)))
    assert counters == {"active": 0, "peak": 1}
    assert len(starts) == 6
    assert all(second - first >= 0.025 for first, second in zip(starts, starts[1:]))
