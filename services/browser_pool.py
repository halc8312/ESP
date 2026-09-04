"""
Browser pool orchestration and page-task helpers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Awaitable, Callable

from services.browser_runtime import (
    BrowserRuntimeConfig,
    SharedBrowserRuntime,
    get_async_playwright_factory,
    normalize_automation_backend,
)
from utils.env_helpers import env_flag as _env_flag


_POOL_LOCK = threading.RLock()
_RUNTIMES: dict[str, SharedBrowserRuntime] = {}
logger = logging.getLogger("browser_pool")

_DEFAULT_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
)

_WARM_RUNTIME_PROFILES: dict[str, dict[str, Any]] = {
    # Keep prewarming consistent with the site-specific fetch adapter. Without
    # this, adding Record City to BROWSER_POOL_WARM_SITES would start vanilla
    # Playwright first and the later Patchright request would fail fast.
    "recordcity": {
        "launch_args": (),
        "headless": True,
        "automation_backend": "patchright",
        "channel": "chromium",
    },
}


def _site_env_name(site: str, suffix: str) -> str:
    return f"{site.upper()}_{suffix}"


def _shared_runtime_enabled(site: str) -> bool:
    site_specific = os.environ.get(_site_env_name(site, "BROWSER_POOL_ENABLED"))
    if site_specific is not None:
        return _env_flag(_site_env_name(site, "BROWSER_POOL_ENABLED"))
    return _env_flag("ENABLE_SHARED_BROWSER_RUNTIME", default=False)


def _shared_runtime_headless(site: str) -> bool:
    site_specific = os.environ.get(_site_env_name(site, "BROWSER_POOL_HEADLESS"))
    if site_specific is not None:
        return _env_flag(_site_env_name(site, "BROWSER_POOL_HEADLESS"), default=True)
    return _env_flag("BROWSER_POOL_HEADLESS", default=True)


def _get_runtime_startup_timeout_seconds() -> float:
    raw = os.environ.get("BROWSER_POOL_STARTUP_TIMEOUT_SECONDS", "60")
    try:
        return max(5.0, float(raw))
    except (TypeError, ValueError):
        return 60.0


def _get_runtime_restart_attempts() -> int:
    raw = os.environ.get("BROWSER_POOL_RESTART_ATTEMPTS", "1")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1


def _get_max_in_flight_tasks(site: str) -> int:
    raw = os.environ.get(_site_env_name(site, "BROWSER_POOL_MAX_CONTEXTS")) or os.environ.get(
        "BROWSER_POOL_MAX_CONTEXTS",
        "1",
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _get_max_tasks_before_restart(site: str) -> int:
    raw = os.environ.get(_site_env_name(site, "BROWSER_POOL_MAX_TASKS_BEFORE_RESTART")) or os.environ.get(
        "BROWSER_POOL_MAX_TASKS_BEFORE_RESTART",
        "0",
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _get_max_runtime_seconds(site: str) -> float:
    raw = os.environ.get(_site_env_name(site, "BROWSER_POOL_MAX_RUNTIME_SECONDS")) or os.environ.get(
        "BROWSER_POOL_MAX_RUNTIME_SECONDS",
        "0",
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _resolve_launch_args(site: str, launch_args: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    # ``[]`` is meaningful for patched drivers: it asks them to use only their
    # own carefully selected defaults. ``None`` keeps ESP's regular defaults.
    if launch_args is not None:
        return tuple(launch_args)

    env_raw = os.environ.get(_site_env_name(site, "BROWSER_POOL_ARGS")) or os.environ.get("BROWSER_POOL_ARGS")
    if env_raw:
        return tuple(arg.strip() for arg in str(env_raw).split(",") if arg.strip())

    return _DEFAULT_LAUNCH_ARGS


def get_browser_runtime(
    site: str,
    *,
    launch_args: list[str] | tuple[str, ...] | None = None,
    headless: bool | None = None,
    automation_backend: str = "playwright",
    channel: str | None = None,
    persistent_user_data_dir: str | None = None,
    persistent_context_options: dict[str, Any] | None = None,
) -> SharedBrowserRuntime | None:
    if not _shared_runtime_enabled(site):
        return None

    normalized_site = str(site or "default").strip().lower()
    normalized_backend = normalize_automation_backend(automation_backend)
    with _POOL_LOCK:
        runtime = _RUNTIMES.get(normalized_site)
        if runtime is not None:
            if (
                runtime.config.automation_backend != normalized_backend
                or runtime.config.channel != channel
                or runtime.config.persistent_user_data_dir != persistent_user_data_dir
                or runtime.config.persistent_context_options
                != dict(persistent_context_options or {})
            ):
                raise RuntimeError(
                    f"Browser runtime profile for {normalized_site} is already active "
                    f"({runtime.config.automation_backend}/{runtime.config.channel or 'default'})"
                )
            return runtime

        runtime = SharedBrowserRuntime(
            BrowserRuntimeConfig(
                site=normalized_site,
                automation_backend=normalized_backend,
                channel=channel,
                headless=_shared_runtime_headless(normalized_site) if headless is None else bool(headless),
                launch_args=_resolve_launch_args(normalized_site, launch_args),
                startup_timeout_seconds=_get_runtime_startup_timeout_seconds(),
                restart_attempts=_get_runtime_restart_attempts(),
                max_in_flight_tasks=_get_max_in_flight_tasks(normalized_site),
                max_tasks_before_restart=_get_max_tasks_before_restart(normalized_site),
                max_runtime_seconds=_get_max_runtime_seconds(normalized_site),
                persistent_user_data_dir=persistent_user_data_dir,
                persistent_context_options=dict(persistent_context_options or {}),
            )
        )
        _RUNTIMES[normalized_site] = runtime
        return runtime


def warm_browser_pool(sites: list[str] | tuple[str, ...] | None = None) -> list[str]:
    warmed_sites: list[str] = []
    target_sites = list(sites or _get_warm_sites())
    for site in target_sites:
        normalized_site = str(site or "").strip().lower()
        runtime = get_browser_runtime(
            normalized_site,
            **_WARM_RUNTIME_PROFILES.get(normalized_site, {}),
        )
        if runtime is None:
            continue
        runtime.start()
        warmed_sites.append(site)
    if warmed_sites:
        logger.info("Warmed browser pool for sites=%s", ",".join(warmed_sites))
    return warmed_sites


def close_browser_pool() -> None:
    with _POOL_LOCK:
        runtimes = list(_RUNTIMES.items())
        _RUNTIMES.clear()

    for _, runtime in runtimes:
        runtime.close()


def _get_warm_sites() -> list[str]:
    raw = str(os.environ.get("BROWSER_POOL_WARM_SITES", "mercari") or "").strip()
    return [site.strip().lower() for site in raw.split(",") if site.strip()]


def get_browser_pool_health() -> dict[str, Any]:
    with _POOL_LOCK:
        runtimes = {
            site: runtime.snapshot()
            for site, runtime in _RUNTIMES.items()
        }
    return {
        "shared_runtime_default_enabled": _env_flag("ENABLE_SHARED_BROWSER_RUNTIME", default=False),
        "warm_sites": _get_warm_sites(),
        "runtimes": runtimes,
    }


async def _execute_page_task(
    browser,
    task_coro_factory: Callable[[Any, Any], Awaitable[Any]],
    *,
    context_options: dict[str, Any] | None = None,
    init_scripts: list[str] | tuple[str, ...] | None = None,
):
    context = await browser.new_context(**(context_options or {}))
    try:
        page = await context.new_page()
        for script in init_scripts or ():
            await page.add_init_script(script)
        return await task_coro_factory(page, context)
    finally:
        await context.close()


async def _run_with_temporary_browser(
    task_coro_factory: Callable[[Any, Any], Awaitable[Any]],
    *,
    launch_args: list[str] | tuple[str, ...] | None = None,
    headless: bool = True,
    context_options: dict[str, Any] | None = None,
    init_scripts: list[str] | tuple[str, ...] | None = None,
    automation_backend: str = "playwright",
    channel: str | None = None,
):
    async_playwright = get_async_playwright_factory(automation_backend)
    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": headless,
            "args": list(_DEFAULT_LAUNCH_ARGS if launch_args is None else launch_args),
        }
        if channel:
            launch_options["channel"] = channel
        browser = await playwright.chromium.launch(**launch_options)
        try:
            return await _execute_page_task(
                browser,
                task_coro_factory,
                context_options=context_options,
                init_scripts=init_scripts,
            )
        finally:
            await browser.close()


async def _execute_persistent_page_task(
    context,
    task_coro_factory: Callable[[Any, Any], Awaitable[Any]],
    *,
    init_scripts: list[str] | tuple[str, ...] | None = None,
):
    """Run one task in a retained tab and persistent owning context.

    Keeping the tab preserves same-site page history across a listing/detail
    sequence.  The site adapter owns cleanup of listeners and routes it adds;
    if a task raises before cleanup, the tab is closed and the next task gets a
    clean one while cookies, local storage, service workers, and profile state
    remain in the context.
    """
    pages = list(getattr(context, "pages", ()) or ())
    page = next(
        (
            candidate
            for candidate in reversed(pages)
            if not callable(getattr(candidate, "is_closed", None))
            or not candidate.is_closed()
        ),
        None,
    )
    if page is None:
        page = await context.new_page()
    try:
        for script in init_scripts or ():
            await page.add_init_script(script)
        return await task_coro_factory(page, context)
    except Exception:
        try:
            await page.close()
        except Exception:
            pass
        raise


async def _run_with_temporary_persistent_context(
    task_coro_factory: Callable[[Any, Any], Awaitable[Any]],
    *,
    user_data_dir: str,
    launch_args: list[str] | tuple[str, ...] | None = None,
    headless: bool = False,
    context_options: dict[str, Any] | None = None,
    init_scripts: list[str] | tuple[str, ...] | None = None,
    automation_backend: str = "patchright",
    channel: str | None = "chrome",
):
    async_playwright = get_async_playwright_factory(automation_backend)
    async with async_playwright() as playwright:
        launch_options = dict(context_options or {})
        launch_options.update(
            {
                "headless": headless,
                "args": list(_DEFAULT_LAUNCH_ARGS if launch_args is None else launch_args),
            }
        )
        if channel:
            launch_options["channel"] = channel
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            **launch_options,
        )
        try:
            return await _execute_persistent_page_task(
                context,
                task_coro_factory,
                init_scripts=init_scripts,
            )
        finally:
            await context.close()


async def run_browser_page_task(
    site: str,
    task_coro_factory: Callable[[Any, Any], Awaitable[Any]],
    *,
    launch_args: list[str] | tuple[str, ...] | None = None,
    headless: bool = True,
    context_options: dict[str, Any] | None = None,
    init_scripts: list[str] | tuple[str, ...] | None = None,
    automation_backend: str = "playwright",
    channel: str | None = None,
):
    runtime = get_browser_runtime(
        site,
        launch_args=launch_args,
        headless=headless,
        automation_backend=automation_backend,
        channel=channel,
    )
    if runtime is None:
        return await _run_with_temporary_browser(
            task_coro_factory,
            launch_args=launch_args,
            headless=headless,
            context_options=context_options,
            init_scripts=init_scripts,
            automation_backend=automation_backend,
            channel=channel,
        )

    future = runtime.submit(
        lambda browser: _execute_page_task(
            browser,
            task_coro_factory,
            context_options=context_options,
            init_scripts=init_scripts,
        )
    )
    return await asyncio.wrap_future(future)


async def run_persistent_browser_page_task(
    site: str,
    task_coro_factory: Callable[[Any, Any], Awaitable[Any]],
    *,
    user_data_dir: str,
    launch_args: list[str] | tuple[str, ...] | None = None,
    headless: bool = False,
    context_options: dict[str, Any] | None = None,
    init_scripts: list[str] | tuple[str, ...] | None = None,
    automation_backend: str = "patchright",
    channel: str | None = "chrome",
):
    """Run a page task in a site-scoped persistent browser context.

    This is opt-in and uses the same lifecycle registry as ordinary runtimes,
    so ``close_browser_pool`` also drains it.  Existing marketplace callers
    continue through ``run_browser_page_task`` and retain fresh contexts.
    """
    persistent_options = dict(context_options or {})
    runtime = get_browser_runtime(
        site,
        launch_args=launch_args,
        headless=headless,
        automation_backend=automation_backend,
        channel=channel,
        persistent_user_data_dir=user_data_dir,
        persistent_context_options=persistent_options,
    )
    if runtime is None:
        return await _run_with_temporary_persistent_context(
            task_coro_factory,
            user_data_dir=user_data_dir,
            launch_args=launch_args,
            headless=headless,
            context_options=persistent_options,
            init_scripts=init_scripts,
            automation_backend=automation_backend,
            channel=channel,
        )

    future = runtime.submit(
        lambda context: _execute_persistent_page_task(
            context,
            task_coro_factory,
            init_scripts=init_scripts,
        )
    )
    return await asyncio.wrap_future(future)
