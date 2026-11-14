"""Execute production Lua in fakeredis/Lua; not a real Redis integration gate."""
import time

import fakeredis
import pytest

from services import marketplace_access as access


@pytest.fixture
def stores():
    server = fakeredis.FakeServer()
    clients = [fakeredis.FakeRedis(server=server), fakeredis.FakeRedis(server=server)]
    try:
        yield [access.RedisAccessStore("", client=client) for client in clients]
    finally:
        for client in clients:
            client.close()


def admit(store, owner):
    return store.acquire("recordcity", owner, interval=0, concurrency=1,
                         lease_seconds=180, consume=False)


def test_production_lua_quarantine_does_not_expire_with_lease_and_clear_is_generation_owned(stores):
    first, second = stores
    assert admit(first, "old:token") == (0, "")
    first.quarantine("recordcity", "old:token")
    first.client.delete(first._keys("recordcity")[0])  # Model expiry, not production deletion.
    assert first.client.ttl(first._keys("recordcity")[3]) == -1
    with pytest.raises(access.AccessLeaseLost):
        admit(second, "new:token")
    assert second.clear_generation_quarantine("recordcity", "wrong") == 0
    assert first.clear_generation_quarantine("recordcity", "old") == 1
    assert admit(second, "new:token") == (0, "")
    second.quarantine("recordcity", "new:token")
    first.clear_generation_quarantine("recordcity", "old")
    first.release("recordcity", "old:token")
    assert first.client.sismember(first._keys("recordcity")[3], "new:token")
    assert first.client.zscore(first._keys("recordcity")[0], "new:token") is not None


def test_generation_clear_matches_full_prefix_and_owner_clear_leaves_other_markers(stores):
    store = stores[0]
    for owner in ("one:a", "one:b", "one-other:a", "two:a"):
        store.quarantine("recordcity", owner)
    assert store.clear_generation_quarantine("recordcity", "one") == 2
    assert store.inspect_quarantines("recordcity") == ["one-other:a", "two:a"]
    assert store.clear_owner_quarantine("recordcity", "two:a") == 1
    assert store.inspect_quarantines("recordcity") == ["one-other:a"]


def test_deadline_capped_dedicated_connection_does_not_mutate_shared_pool(stores):
    store = stores[0]
    options = dict(store.client.connection_pool.connection_kwargs)
    store.quarantine("recordcity", "one:token")
    assert store.clear_generation_quarantine("recordcity", "one", deadline=time.monotonic() + 1) == 1
    assert store.client.connection_pool.connection_kwargs == options
    store.quarantine("recordcity", "two:token")
    with pytest.raises(access.AccessStoreUnavailable):
        store.clear_generation_quarantine("recordcity", "two", deadline=time.monotonic() - 1)
    assert store.inspect_quarantines("recordcity") == ["two:token"]
