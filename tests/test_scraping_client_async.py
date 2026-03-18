import asyncio

import pytest

from services.scraping_client import gather_with_concurrency, get_async_fetch_settings, run_coro_sync


@pytest.mark.asyncio
async def test_gather_with_concurrency_preserves_input_order_with_out_of_order_completion():
    async def worker(value):
        await asyncio.sleep({1: 0.03, 2: 0.01, 3: 0.02}[value])
        return value * 10

    results = await gather_with_concurrency([1, 2, 3], worker, concurrency=3)

    assert results == [10, 20, 30]


@pytest.mark.asyncio
async def test_gather_with_concurrency_returns_exceptions_and_respects_cap():
    state = {"active": 0, "max_active": 0}

    async def worker(value):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        if value == "boom":
            raise RuntimeError("expected failure")
        return value.upper()

    results = await gather_with_concurrency(["a", "boom", "c"], worker, concurrency=2)

    assert results[0] == "A"
    assert isinstance(results[1], RuntimeError)
    assert results[2] == "C"
    assert state["max_active"] <= 2


def test_get_async_fetch_settings_uses_site_defaults_and_env_override(monkeypatch):
    mercari_settings = get_async_fetch_settings("mercari")
    assert mercari_settings.concurrency == 1

    monkeypatch.setenv("RAKUMA_DETAIL_CONCURRENCY", "6")
    monkeypatch.setenv("RAKUMA_DETAIL_TIMEOUT", "25")
    monkeypatch.setenv("RAKUMA_DETAIL_RETRIES", "2")
    monkeypatch.setenv("RAKUMA_DETAIL_BACKOFF", "1.25")

    rakuma_settings = get_async_fetch_settings("rakuma")

    assert rakuma_settings.concurrency == 6
    assert rakuma_settings.timeout == 25
    assert rakuma_settings.retries == 2
    assert rakuma_settings.backoff_seconds == 1.25


def test_run_coro_sync_propagates_exceptions():
    async def boom():
        await asyncio.sleep(0)
        raise ValueError("bad coroutine")

    with pytest.raises(ValueError, match="bad coroutine"):
        run_coro_sync(boom())
