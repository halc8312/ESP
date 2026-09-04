import asyncio
from concurrent.futures import Future

import pytest

import services.browser_pool as browser_pool
from services.browser_pool import (
    close_browser_pool,
    get_browser_pool_health,
    get_browser_runtime,
    run_browser_page_task,
    run_persistent_browser_page_task,
    warm_browser_pool,
)
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

    fake_runtime = FakeRuntime()

    def fake_get_browser_runtime(site, **kwargs):
        captured["runtime_site"] = site
        captured["runtime_kwargs"] = kwargs
        return fake_runtime

    monkeypatch.setattr("services.browser_pool.get_browser_runtime", fake_get_browser_runtime)

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
    assert captured["runtime_site"] == "mercari"
    assert captured["runtime_kwargs"]["automation_backend"] == "playwright"
    assert captured["runtime_kwargs"]["channel"] is None


def test_shared_runtime_reuses_browser_with_fresh_context_per_page_task(monkeypatch):
    monkeypatch.setenv("ENABLE_SHARED_BROWSER_RUNTIME", "1")

    class FakeContext:
        def __init__(self, sequence):
            self.sequence = sequence
            self.closed = False

        async def new_page(self):
            return object()

        async def close(self):
            self.closed = True

    class FakeBrowser:
        def __init__(self):
            self.contexts = []

        async def new_context(self, **_context_options):
            context = FakeContext(len(self.contexts) + 1)
            self.contexts.append(context)
            return context

    class FakeRuntime:
        def __init__(self):
            self.browser = FakeBrowser()
            self.submitted_browsers = []

        def submit(self, coro_factory):
            future = Future()
            self.submitted_browsers.append(self.browser)
            future.set_result(run_coro_sync(coro_factory(self.browser)))
            return future

    runtime = FakeRuntime()
    runtime_sites = []

    def fake_get_browser_runtime(site, **_kwargs):
        runtime_sites.append(site)
        return runtime

    monkeypatch.setattr(
        "services.browser_pool.get_browser_runtime",
        fake_get_browser_runtime,
    )

    first_context = run_coro_sync(
        run_browser_page_task(
            "recordcity_headful",
            lambda _page, context: asyncio.sleep(0, result=context),
        )
    )
    second_context = run_coro_sync(
        run_browser_page_task(
            "recordcity_headful",
            lambda _page, context: asyncio.sleep(0, result=context),
        )
    )

    assert runtime_sites == ["recordcity_headful", "recordcity_headful"]
    assert runtime.submitted_browsers == [runtime.browser, runtime.browser]
    assert runtime.browser.contexts == [first_context, second_context]
    assert first_context is not second_context
    assert first_context.closed is True
    assert second_context.closed is True


def test_persistent_runtime_reuses_context_and_page_state(monkeypatch):
    captured = {}

    class FakePage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.pages_created = []
            self.pages = []
            self.state = {}

        async def new_page(self):
            page = FakePage()
            self.pages_created.append(page)
            self.pages.append(page)
            return page

    class FakeRuntime:
        def __init__(self):
            self.context = FakeContext()

        def submit(self, coro_factory):
            future = Future()
            future.set_result(run_coro_sync(coro_factory(self.context)))
            return future

    runtime = FakeRuntime()

    def fake_get_browser_runtime(site, **kwargs):
        captured.setdefault("runtime_calls", []).append({"site": site, **kwargs})
        return runtime

    monkeypatch.setattr(browser_pool, "get_browser_runtime", fake_get_browser_runtime)

    async def first_task(page, context):
        context.state["local_storage_marker"] = "retained"
        return id(context), id(page)

    async def second_task(page, context):
        return id(context), id(page), context.state["local_storage_marker"]

    first_context_id, first_page_id = run_coro_sync(
        run_persistent_browser_page_task(
            "recordcity_persistent_chrome",
            first_task,
            user_data_dir="/tmp/recordcity-profile",
            launch_args=[],
            context_options={"no_viewport": True},
            automation_backend="patchright",
            channel="chrome",
        )
    )
    second_context_id, second_page_id, marker = run_coro_sync(
        run_persistent_browser_page_task(
            "recordcity_persistent_chrome",
            second_task,
            user_data_dir="/tmp/recordcity-profile",
            launch_args=[],
            context_options={"no_viewport": True},
            automation_backend="patchright",
            channel="chrome",
        )
    )

    assert first_context_id == second_context_id == id(runtime.context)
    assert first_page_id == second_page_id
    assert marker == "retained"
    assert len(runtime.context.pages_created) == 1
    assert runtime.context.pages_created[0].closed is False
    assert captured["runtime_calls"][0] == {
        "site": "recordcity_persistent_chrome",
        "launch_args": [],
        "headless": False,
        "automation_backend": "patchright",
        "channel": "chrome",
        "persistent_user_data_dir": "/tmp/recordcity-profile",
        "persistent_context_options": {"no_viewport": True},
    }


def test_persistent_runtime_discards_tab_when_task_cleanup_cannot_complete(monkeypatch):
    class FakePage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.page = FakePage()
            self.pages = [self.page]

        async def new_page(self):
            raise AssertionError("the retained tab should be selected")

    context = FakeContext()

    class FakeRuntime:
        def submit(self, coro_factory):
            future = Future()
            try:
                future.set_result(run_coro_sync(coro_factory(context)))
            except Exception as exc:
                future.set_exception(exc)
            return future

    monkeypatch.setattr(
        browser_pool,
        "get_browser_runtime",
        lambda *_args, **_kwargs: FakeRuntime(),
    )

    async def failing_task(_page, _context):
        raise RuntimeError("adapter cleanup interrupted")

    with pytest.raises(RuntimeError, match="cleanup interrupted"):
        run_coro_sync(
            run_persistent_browser_page_task(
                "recordcity_persistent_chrome",
                failing_task,
                user_data_dir="/tmp/recordcity-profile",
            )
        )

    assert context.page.closed is True


def test_persistent_task_uses_temporary_owned_context_when_pool_is_disabled(
    monkeypatch,
):
    captured = {}

    class FakePage:
        def __init__(self):
            self.scripts = []

        async def add_init_script(self, script):
            self.scripts.append(script)

    class FakeContext:
        def __init__(self):
            self.pages = []
            self.page = FakePage()

        async def new_page(self):
            self.pages.append(self.page)
            return self.page

        async def close(self):
            captured["context_closed"] = True

    class FakeBrowserType:
        async def launch_persistent_context(self, user_data_dir, **options):
            captured["user_data_dir"] = user_data_dir
            captured["launch_options"] = options
            captured["context"] = FakeContext()
            return captured["context"]

    class FakePlaywright:
        chromium = FakeBrowserType()

    class FakeManager:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *_args):
            captured["manager_closed"] = True

    monkeypatch.setattr(browser_pool, "get_browser_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(
        browser_pool,
        "get_async_playwright_factory",
        lambda backend: FakeManager,
    )

    async def task(page, context):
        return page.scripts, context is captured["context"]

    result = run_coro_sync(
        run_persistent_browser_page_task(
            "recordcity_persistent_chrome",
            task,
            user_data_dir="/tmp/recordcity-profile",
            launch_args=[],
            headless=False,
            context_options={"no_viewport": True},
            init_scripts=["window.__recordcity = true"],
            automation_backend="patchright",
            channel="chrome",
        )
    )

    assert result == (["window.__recordcity = true"], True)
    assert captured["user_data_dir"] == "/tmp/recordcity-profile"
    assert captured["launch_options"] == {
        "no_viewport": True,
        "headless": False,
        "args": [],
        "channel": "chrome",
    }
    assert captured["context_closed"] is True
    assert captured["manager_closed"] is True


def test_run_browser_page_task_propagates_recordcity_profile_to_shared_runtime(monkeypatch):
    captured = {}

    class FakeContext:
        async def new_page(self):
            return object()

        async def close(self):
            captured["context_closed"] = True

    class FakeBrowser:
        async def new_context(self, **context_options):
            captured["context_options"] = context_options
            return FakeContext()

    class FakeRuntime:
        def submit(self, coro_factory):
            future = Future()
            future.set_result(run_coro_sync(coro_factory(FakeBrowser())))
            return future

    def fake_get_browser_runtime(site, **kwargs):
        captured["site"] = site
        captured["runtime_kwargs"] = kwargs
        return FakeRuntime()

    monkeypatch.setattr(
        "services.browser_pool.get_browser_runtime",
        fake_get_browser_runtime,
    )

    result = run_coro_sync(
        run_browser_page_task(
            "recordcity",
            lambda page, context: asyncio.sleep(0, result="shared-patchright-ok"),
            launch_args=[],
            context_options={"locale": "ja-JP"},
            automation_backend="patchright",
            channel="chromium",
        )
    )

    assert result == "shared-patchright-ok"
    assert captured["site"] == "recordcity"
    assert captured["runtime_kwargs"] == {
        "launch_args": [],
        "headless": True,
        "automation_backend": "patchright",
        "channel": "chromium",
    }
    assert captured["context_options"] == {"locale": "ja-JP"}
    assert captured["context_closed"] is True


def test_run_browser_page_task_falls_back_to_temporary_browser(monkeypatch):
    monkeypatch.delenv("ENABLE_SHARED_BROWSER_RUNTIME", raising=False)
    monkeypatch.setattr("services.browser_pool.get_browser_runtime", lambda *args, **kwargs: None)
    captured = {}

    async def fake_run_with_temporary_browser(task_coro_factory, **kwargs):
        captured["kwargs"] = kwargs

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
    assert captured["kwargs"]["automation_backend"] == "playwright"
    assert captured["kwargs"]["channel"] is None


def test_temporary_browser_uses_recordcity_patchright_profile(monkeypatch):
    captured = {}

    class FakePage:
        pass

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def new_page(self):
            return FakePage()

        async def close(self):
            self.closed = True
            captured["context_closed"] = True

    class FakeBrowser:
        async def new_context(self, **context_options):
            captured["context_options"] = context_options
            return FakeContext()

        async def close(self):
            captured["browser_closed"] = True

    class FakeBrowserType:
        async def launch(self, **launch_options):
            captured["launch_options"] = launch_options
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeBrowserType()

    class FakePlaywrightManager:
        async def __aenter__(self):
            captured["factory_entered"] = True
            return FakePlaywright()

        async def __aexit__(self, exc_type, exc, traceback):
            captured["factory_exited"] = True

    def fake_get_async_playwright_factory(backend):
        captured["backend"] = backend
        return FakePlaywrightManager

    monkeypatch.setattr(
        browser_pool,
        "get_async_playwright_factory",
        fake_get_async_playwright_factory,
    )

    result = run_coro_sync(
        browser_pool._run_with_temporary_browser(
            lambda page, context: asyncio.sleep(0, result="patchright-ok"),
            launch_args=[],
            headless=True,
            context_options={"locale": "ja-JP"},
            automation_backend="patchright",
            channel="chromium",
        )
    )

    assert result == "patchright-ok"
    assert captured["backend"] == "patchright"
    assert captured["factory_entered"] is True
    assert captured["launch_options"] == {
        "headless": True,
        "args": [],
        "channel": "chromium",
    }
    assert captured["context_options"] == {"locale": "ja-JP"}
    assert captured["context_closed"] is True
    assert captured["browser_closed"] is True
    assert captured["factory_exited"] is True


def test_get_browser_runtime_uses_recordcity_patchright_profile(monkeypatch):
    monkeypatch.setenv("ENABLE_SHARED_BROWSER_RUNTIME", "1")
    close_browser_pool()

    try:
        runtime = get_browser_runtime(
            "recordcity",
            launch_args=[],
            automation_backend="patchright",
            channel="chromium",
        )
        health = get_browser_pool_health()

        assert runtime is not None
        assert runtime.config.automation_backend == "patchright"
        assert runtime.config.channel == "chromium"
        assert runtime.config.launch_args == ()
        assert health["runtimes"]["recordcity"]["automation_backend"] == "patchright"
        assert health["runtimes"]["recordcity"]["channel"] == "chromium"
    finally:
        close_browser_pool()


def test_get_browser_runtime_rejects_same_site_profile_mismatch(monkeypatch):
    monkeypatch.setenv("ENABLE_SHARED_BROWSER_RUNTIME", "1")
    close_browser_pool()

    try:
        runtime = get_browser_runtime(
            "recordcity",
            launch_args=[],
            automation_backend="patchright",
            channel="chromium",
        )
        assert runtime is not None

        with pytest.raises(RuntimeError, match="already active"):
            get_browser_runtime(
                "recordcity",
                launch_args=[],
                automation_backend="playwright",
                channel="chromium",
            )

        with pytest.raises(RuntimeError, match="already active"):
            get_browser_runtime(
                "recordcity",
                launch_args=[],
                automation_backend="patchright",
                channel=None,
            )
    finally:
        close_browser_pool()


def test_warm_browser_pool_uses_recordcity_site_profile(monkeypatch):
    captured = {}

    class FakeRuntime:
        def start(self):
            captured["started"] = True

    def fake_get_browser_runtime(site, **kwargs):
        captured["site"] = site
        captured["kwargs"] = kwargs
        return FakeRuntime()

    monkeypatch.setattr(browser_pool, "get_browser_runtime", fake_get_browser_runtime)

    assert warm_browser_pool(["RecordCity"]) == ["RecordCity"]
    assert captured == {
        "site": "recordcity",
        "kwargs": {
            "launch_args": (),
            "headless": True,
            "automation_backend": "patchright",
            "channel": "chromium",
        },
        "started": True,
    }


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
