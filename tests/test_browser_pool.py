import asyncio
from concurrent.futures import Future

from services.browser_pool import close_browser_pool, get_browser_pool_health, get_browser_runtime, run_browser_page_task
from services.scraping_client import run_coro_sync


def test_run_browser_page_task_uses_shared_runtime(monkeypatch):
    monkeypatch.setenv("ENABLE_SHARED_BROWSER_RUNTIME", "1")
    captured = {}

    class FakePage:
        def __init__(self):
            self.scripts = []

        async def add_init_script(self, script):
            self.scripts.append(script.strip())

    class FakeContext:
        def __init__(self):
            self.page = FakePage()
            self.closed = False

        async def new_page(self):
            return self.page

        async def close(self):
            self.closed = True

    class FakeBrowser:
        def __init__(self):
            self.context_options = None
            self.context = FakeContext()

        async def new_context(self, **context_options):
            self.context_options = context_options
            return self.context

    class FakeRuntime:
        def submit(self, coro_factory):
            future = Future()

            async def _runner():
                browser = FakeBrowser()
                result = await coro_factory(browser)
                captured["scripts"] = browser.context.page.scripts
                captured["context_options"] = browser.context_options
                captured["context_closed"] = browser.context.closed
                return result

            future.set_result(run_coro_sync(_runner()))
            return future

    monkeypatch.setattr("services.browser_pool.get_browser_runtime", lambda *args, **kwargs: FakeRuntime())

    result = run_coro_sync(
        run_browser_page_task(
            "mercari",
            lambda page, context: asyncio.sleep(0, result={"ok": True, "scripts": list(page.scripts)}),
            context_options={"user_agent": "ua"},
            init_scripts=["window.__esp = true;"],
        )
    )

    assert result["ok"] is True
    assert captured["context_options"]["user_agent"] == "ua"
    assert captured["scripts"] == ["window.__esp = true;"]
    assert captured["context_closed"] is True


def test_run_browser_page_task_falls_back_to_temporary_browser(monkeypatch):
    monkeypatch.delenv("ENABLE_SHARED_BROWSER_RUNTIME", raising=False)
    monkeypatch.setattr("services.browser_pool.get_browser_runtime", lambda *args, **kwargs: None)

    async def fake_run_with_temporary_browser(task_coro_factory, **kwargs):
        captured = {"kwargs": kwargs}

        class FakePage:
            scripts = []

            async def add_init_script(self, script):
                self.scripts.append(script)

        class FakeContext:
            async def new_page(self):
                return FakePage()

            async def close(self):
                return None

        class FakeBrowser:
            async def new_context(self, **context_options):
                captured["context_options"] = context_options
                return FakeContext()

        return await task_coro_factory(FakePage(), FakeContext())

    monkeypatch.setattr("services.browser_pool._run_with_temporary_browser", fake_run_with_temporary_browser)

    result = run_coro_sync(
        run_browser_page_task(
            "mercari",
            lambda page, context: asyncio.sleep(0, result="temp-ok"),
            context_options={"user_agent": "ua"},
        )
    )

    assert result == "temp-ok"


def test_get_browser_runtime_uses_site_max_context_limit(monkeypatch):
    monkeypatch.setenv("ENABLE_SHARED_BROWSER_RUNTIME", "1")
    monkeypatch.setenv("MERCARI_BROWSER_POOL_MAX_CONTEXTS", "3")
    close_browser_pool()

    runtime = get_browser_runtime("mercari")
    health = get_browser_pool_health()

    assert runtime is not None
    assert runtime.config.max_in_flight_tasks == 3
    assert health["runtimes"]["mercari"]["max_in_flight_tasks"] == 3
    assert health["runtimes"]["mercari"]["state"] == "idle"

    close_browser_pool()


def test_get_browser_runtime_uses_recycle_policy_env(monkeypatch):
    monkeypatch.setenv("ENABLE_SHARED_BROWSER_RUNTIME", "1")
    monkeypatch.setenv("MERCARI_BROWSER_POOL_MAX_TASKS_BEFORE_RESTART", "7")
    monkeypatch.setenv("MERCARI_BROWSER_POOL_MAX_RUNTIME_SECONDS", "900")
    close_browser_pool()

    runtime = get_browser_runtime("mercari")
    health = get_browser_pool_health()

    assert runtime is not None
    assert runtime.config.max_tasks_before_restart == 7
    assert runtime.config.max_runtime_seconds == 900.0
    assert health["runtimes"]["mercari"]["max_tasks_before_restart"] == 7
    assert health["runtimes"]["mercari"]["max_runtime_seconds"] == 900.0

    close_browser_pool()
