import asyncio
import time

import pytest

from services.browser_runtime import BrowserRuntimeConfig, SharedBrowserRuntime


def test_shared_browser_runtime_snapshot_reports_idle_then_closed():
    runtime = SharedBrowserRuntime(
        BrowserRuntimeConfig(
            site="mercari",
            max_in_flight_tasks=2,
            restart_attempts=3,
        )
    )

    initial = runtime.snapshot()
    assert initial["site"] == "mercari"
    assert initial["state"] == "idle"
    assert initial["restart_count"] == 0
    assert initial["submit_count"] == 0
    assert initial["max_in_flight_tasks"] == 2
    assert initial["max_tasks_before_restart"] == 0
    assert initial["max_runtime_seconds"] == 0.0
    assert initial["active_tasks"] == 0
    assert initial["completed_tasks_since_launch"] == 0
    assert initial["thread_alive"] is False
    assert initial["browser_ready"] is False

    runtime.close()

    closed = runtime.snapshot()
    assert closed["state"] == "closed"
    assert closed["thread_alive"] is False
    assert closed["browser_ready"] is False


def test_shared_browser_runtime_recycles_after_task_threshold(monkeypatch):
    runtime = SharedBrowserRuntime(
        BrowserRuntimeConfig(
            site="mercari",
            max_tasks_before_restart=2,
        )
    )
    launches = []

    async def fake_launch_async():
        launches.append(object())
        runtime._browser = launches[-1]
        with runtime._lock:
            runtime._state = "ready"
            runtime._last_started_at = time.time()
            runtime._last_error = None
            runtime._completed_tasks_since_launch = 0

    async def fake_shutdown_async():
        runtime._browser = None
        with runtime._lock:
            if not runtime._closed and runtime._state != "restarting":
                runtime._state = "idle"

    monkeypatch.setattr(runtime, "_launch_async", fake_launch_async)
    monkeypatch.setattr(runtime, "_shutdown_async", fake_shutdown_async)

    async def task(browser):
        return browser

    first = asyncio.run(runtime._run_with_restart(task))
    second = asyncio.run(runtime._run_with_restart(task))
    third = asyncio.run(runtime._run_with_restart(task))

    assert first is second
    assert third is not second
    assert runtime.restart_count == 1
    assert len(launches) == 2


def test_shared_browser_runtime_recycles_after_max_age(monkeypatch):
    runtime = SharedBrowserRuntime(
        BrowserRuntimeConfig(
            site="mercari",
            max_runtime_seconds=1,
        )
    )
    launches = []

    async def fake_launch_async():
        launches.append(object())
        runtime._browser = launches[-1]
        with runtime._lock:
            runtime._state = "ready"
            runtime._last_started_at = time.time()
            runtime._last_error = None
            runtime._completed_tasks_since_launch = 0

    async def fake_shutdown_async():
        runtime._browser = None
        with runtime._lock:
            if not runtime._closed and runtime._state != "restarting":
                runtime._state = "idle"

    monkeypatch.setattr(runtime, "_launch_async", fake_launch_async)
    monkeypatch.setattr(runtime, "_shutdown_async", fake_shutdown_async)

    async def task(browser):
        return browser

    first = asyncio.run(runtime._run_with_restart(task))
    runtime._last_started_at = time.time() - 5
    second = asyncio.run(runtime._run_with_restart(task))

    assert second is not first
    assert runtime.restart_count == 1
    assert len(launches) == 2


def test_shared_browser_runtime_closes_cleanly_after_startup_failure(monkeypatch):
    runtime = SharedBrowserRuntime(
        BrowserRuntimeConfig(site="mercari", startup_timeout_seconds=1)
    )

    async def fail_launch():
        runtime._playwright = object()
        raise RuntimeError("browser executable missing")

    async def fake_shutdown():
        runtime._playwright = None
        runtime._browser = None

    monkeypatch.setattr(runtime, "_launch_async", fail_launch)
    monkeypatch.setattr(runtime, "_shutdown_async", fake_shutdown)

    with pytest.raises(RuntimeError, match="Failed to start browser runtime"):
        runtime.start()

    runtime.close()

    snapshot = runtime.snapshot()
    assert snapshot["state"] == "closed"
    assert snapshot["loop_running"] is False
    assert snapshot["thread_alive"] is False
