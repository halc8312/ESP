"""
Shared Playwright browser runtime for worker-side browser reuse.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


logger = logging.getLogger("browser_runtime")

_SUPPORTED_AUTOMATION_BACKENDS = frozenset({"playwright", "patchright"})


class BrowserCleanupUnverified(RuntimeError):
    """Browser resources may still be active; their admission must stay fenced."""


def cleanup_timeout_seconds() -> float:
    try:
        return min(30.0, max(0.01, float(os.environ.get("BROWSER_CLEANUP_TIMEOUT_SECONDS", "10"))))
    except (TypeError, ValueError):
        return 10.0


async def bounded_browser_cleanup(awaitable, *, label: str, timeout: float | None = None):
    """Wait for actual cleanup completion, even when its caller is cancelled.

    asyncio.wait_for is unsuitable here: cancellation-ignoring browser RPCs can
    make its own cancellation wait unbounded. A pending RPC is deliberately
    reported as unverified, never as a closed context. The owning supervisor
    must kill/reap the process generation before clearing its quarantine.
    """
    task = asyncio.ensure_future(awaitable)
    deadline = asyncio.get_running_loop().time() + (cleanup_timeout_seconds() if timeout is None else timeout)
    cancelled = False
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            task.cancel()
            # Retrieve a late exception without claiming that the RPC stopped.
            task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
            raise BrowserCleanupUnverified(f"Timed out verifying {label}")
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            # Repeated caller cancellation must not interrupt ownership cleanup.
            cancelled = True
            continue
    if task.cancelled():
        raise BrowserCleanupUnverified(f"Cancelled before verifying {label}")
    try:
        result = task.result()
    except Exception as exc:
        raise BrowserCleanupUnverified(f"Failed to verify {label}") from exc
    if cancelled:
        raise asyncio.CancelledError
    return result


def normalize_automation_backend(value: str | None) -> str:
    """Return a supported browser driver name without importing it eagerly."""
    backend = str(value or "playwright").strip().lower()
    if backend not in _SUPPORTED_AUTOMATION_BACKENDS:
        supported = ", ".join(sorted(_SUPPORTED_AUTOMATION_BACKENDS))
        raise ValueError(f"Unsupported browser automation backend: {backend} (expected {supported})")
    return backend


def get_async_playwright_factory(backend: str):
    """Load Playwright or Patchright only when that site's browser starts."""
    backend = normalize_automation_backend(backend)
    if backend == "patchright":
        from patchright.async_api import async_playwright
    else:
        from playwright.async_api import async_playwright
    return async_playwright


@dataclass(frozen=True)
class BrowserRuntimeConfig:
    site: str
    browser_name: str = "chromium"
    automation_backend: str = "playwright"
    channel: str | None = None
    headless: bool = True
    launch_args: tuple[str, ...] = ()
    startup_timeout_seconds: float = 60.0
    restart_attempts: int = 1
    max_in_flight_tasks: int = 1
    max_tasks_before_restart: int = 50
    max_runtime_seconds: float = 900.0
    # When set, the runtime owns Chromium's one persistent BrowserContext
    # instead of an incognito-capable Browser.  Callers that do not opt in keep
    # the existing Browser/new_context lifecycle unchanged.
    persistent_user_data_dir: str | None = None
    persistent_context_options: dict[str, Any] = field(default_factory=dict)


def _looks_like_browser_restart_error(exc: BaseException) -> bool:
    message = str(exc or "").lower()
    return any(
        token in message
        for token in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "connection closed",
            "browser closed",
        )
    )


class SharedBrowserRuntime:
    def __init__(self, config: BrowserRuntimeConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playwright = None
        self._playwright_manager = None
        self._browser = None
        self._startup_error: BaseException | None = None
        self._closed = False
        self._quarantined = False
        self._tasks: set[asyncio.Task] = set()
        self._state = "idle"
        self._last_started_at: float | None = None
        self._last_submit_at: float | None = None
        self._last_completed_at: float | None = None
        self._last_restart_at: float | None = None
        self._last_error: str | None = None
        self._active_tasks = 0
        self._completed_tasks_since_launch = 0
        self.submit_count = 0
        self.restart_count = 0
        self._submit_permit = threading.BoundedSemaphore(max(1, int(self.config.max_in_flight_tasks or 1)))

    def start(self) -> None:
        with self._lock:
            if self._quarantined:
                raise BrowserCleanupUnverified(f"Browser runtime for {self.config.site} is quarantined")
            if self._closed:
                raise RuntimeError(f"Browser runtime for {self.config.site} is closed")
            if self._thread is None or not self._thread.is_alive():
                self._ready_event.clear()
                self._startup_error = None
                self._state = "starting"
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name=f"browser-runtime-{self.config.site}",
                    daemon=True,
                )
                self._thread.start()

        from services.operation_deadline import remaining_seconds
        timeout = remaining_seconds(self.config.startup_timeout_seconds)
        if not self._ready_event.wait(timeout=max(0.0, timeout)):
            self.quarantine(f"Timed out starting browser runtime for {self.config.site}")
            raise BrowserCleanupUnverified(f"Timed out starting browser runtime for {self.config.site}")
        if self._startup_error is not None:
            if self._quarantined:
                raise BrowserCleanupUnverified(f"Startup cleanup unverified for {self.config.site}") from self._startup_error
            with self._lock:
                self._state = "error"
                self._last_error = str(self._startup_error)
            raise RuntimeError(f"Failed to start browser runtime for {self.config.site}") from self._startup_error

    def submit(self, coro_factory: Callable[[Any], Awaitable[Any]]) -> Future:
        self.start()
        if self._loop is None:
            raise RuntimeError(f"Browser runtime for {self.config.site} has no event loop")
        with self._lock:
            if self._quarantined:
                raise BrowserCleanupUnverified(f"Browser runtime for {self.config.site} is quarantined")
        from services.operation_deadline import remaining_seconds
        acquired = self._submit_permit.acquire(timeout=max(0.0, remaining_seconds(self.config.startup_timeout_seconds)))
        if not acquired:
            raise TimeoutError(f"Timed out waiting for browser runtime slot for {self.config.site}")

        with self._lock:
            if self._quarantined:
                self._submit_permit.release()
                raise BrowserCleanupUnverified(f"Browser runtime for {self.config.site} is quarantined")
            self.submit_count += 1
            self._last_submit_at = time.time()
            self._state = "busy"

        future = Future()
        # Unlike Future.done(), this is set only once the actual asyncio Task
        # has stopped (or was cancelled before any Task/resources were started).
        future.execution_finished = threading.Event()
        loop = self._loop

        def _release_permit() -> None:
            with self._lock:
                if self._quarantined:
                    self._state = "quarantined"
                    return
                self._submit_permit.release()
                if self._closed:
                    self._state = "closed"
                elif self._browser is not None:
                    self._state = "busy" if self._active_tasks else "ready"
                else:
                    self._state = "idle"

        def _enqueue() -> None:
            # No coroutine/resources started if the caller cancelled while the
            # callback was queued. Otherwise only the *asyncio Task* finishing
            # can return this slot, not Future.cancel() in the calling thread.
            if future.cancelled():
                future.execution_finished.set()
                _release_permit()
                return
            task = loop.create_task(self._run_with_restart(coro_factory))
            self._tasks.add(task)

            def _cancel(completed):
                if completed.cancelled():
                    try:
                        loop.call_soon_threadsafe(task.cancel)
                    except RuntimeError:
                        self.quarantine("Event loop stopped before cancellation was verified")

            def _done(completed):
                self._tasks.discard(completed)
                future.execution_finished.set()
                _release_permit()
                if completed.cancelled():
                    future.cancel()
                else:
                    error = completed.exception()
                    if not future.done():
                        try:
                            if error is None:
                                future.set_result(completed.result())
                            else:
                                future.set_exception(error)
                        except InvalidStateError:
                            # Cancellation in the caller thread can race this
                            # completion; the actual Task already returned its
                            # permit above and its exception was retrieved.
                            pass

            future.add_done_callback(_cancel)
            task.add_done_callback(_done)

        try:
            loop.call_soon_threadsafe(_enqueue)
        except Exception:
            self._submit_permit.release()
            raise
        return future

    def quarantine(self, reason: str) -> None:
        with self._lock:
            self._quarantined = True
            self._state = "quarantined"
            self._last_error = str(reason)

    def close(self) -> bool:
        with self._lock:
            self._closed = True
            self._state = "closing"
            loop = self._loop
            thread = self._thread

        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                # The startup thread can close its loop while close() is
                # taking the snapshot above.
                pass
        if thread is not None:
            thread.join(timeout=cleanup_timeout_seconds() + 1)
        if thread is not None and thread.is_alive():
            self.quarantine("Runtime thread did not finish cleanup")
            return False

        with self._lock:
            self._thread = None
            self._loop = None
            self._state = "quarantined" if self._quarantined else "closed"
            return not self._quarantined

    async def _launch_async(self) -> None:
        async_playwright = get_async_playwright_factory(self.config.automation_backend)
        self._playwright_manager = async_playwright()
        self._playwright = await self._playwright_manager.start()
        browser_type = getattr(self._playwright, self.config.browser_name)
        launch_options: dict[str, Any] = {
            "headless": self.config.headless,
            "args": list(self.config.launch_args),
        }
        if self.config.channel:
            launch_options["channel"] = self.config.channel
        if self.config.persistent_user_data_dir is not None:
            # Persistent-context options include both browser launch settings
            # and default-context settings in Playwright's API.  Keep the
            # runtime-selected channel/headless/args authoritative so a caller
            # cannot accidentally create a different profile under one key.
            persistent_options = dict(self.config.persistent_context_options)
            persistent_options.update(launch_options)
            self._browser = await browser_type.launch_persistent_context(
                user_data_dir=self.config.persistent_user_data_dir,
                **persistent_options,
            )
        else:
            self._browser = await browser_type.launch(
                **launch_options,
            )
        with self._lock:
            self._state = "quarantined" if self._quarantined else "ready"
            self._last_started_at = time.time()
            if not self._quarantined:
                self._last_error = None
            self._completed_tasks_since_launch = 0

    async def _shutdown_async(self) -> None:
        browser = self._browser
        playwright = self._playwright
        manager = self._playwright_manager
        try:
            if browser is not None:
                await bounded_browser_cleanup(browser.close(), label="browser.close")
                self._browser = None
            if playwright is not None:
                await bounded_browser_cleanup(playwright.stop(), label="playwright.stop")
                self._playwright = None
                self._playwright_manager = None
            elif manager is not None:
                await bounded_browser_cleanup(manager.__aexit__(None, None, None), label="partial playwright startup.stop")
                self._playwright_manager = None
        except BrowserCleanupUnverified as exc:
            self.quarantine(str(exc))
            raise
        with self._lock:
            if not self._closed and not self._quarantined and self._state != "restarting":
                self._state = "idle"

    async def _restart_async(self) -> None:
        with self._lock:
            self.restart_count += 1
            self._last_restart_at = time.time()
            self._state = "restarting"
        await self._launch_async()

    def _planned_restart_reason_locked(self) -> str | None:
        if self._browser is None:
            return None

        max_tasks_before_restart = max(0, int(self.config.max_tasks_before_restart or 0))
        if max_tasks_before_restart > 0 and self._completed_tasks_since_launch >= max_tasks_before_restart:
            return "task-threshold"

        max_runtime_seconds = float(self.config.max_runtime_seconds or 0.0)
        if max_runtime_seconds > 0 and self._last_started_at is not None:
            runtime_age_seconds = time.time() - self._last_started_at
            if runtime_age_seconds >= max_runtime_seconds:
                return "max-age"

        return None

    async def _run_with_restart(self, coro_factory: Callable[[Any], Awaitable[Any]]) -> Any:
        last_error: BaseException | None = None
        attempts = max(0, int(self.config.restart_attempts))
        operation_started = False
        with self._lock:
            self._active_tasks += 1
            self._state = "busy"

        try:
            for attempt_index in range(attempts + 1):
                try:
                    planned_restart_reason = None
                    with self._lock:
                        if self._active_tasks == 1:
                            planned_restart_reason = self._planned_restart_reason_locked()
                    if planned_restart_reason is not None:
                        logger.info(
                            "Recycling shared browser runtime for %s due to %s",
                            self.config.site,
                            planned_restart_reason,
                        )
                        await self._shutdown_async()
                        await self._restart_async()

                    if self._browser is None:
                        await self._launch_async()
                    operation_started = True
                    return await coro_factory(self._browser)
                except BrowserCleanupUnverified as exc:
                    self.quarantine(str(exc))
                    raise
                except Exception as exc:
                    last_error = exc
                    with self._lock:
                        self._last_error = str(exc)
                    if attempt_index >= attempts or not _looks_like_browser_restart_error(exc):
                        with self._lock:
                            self._state = "error"
                        raise
                    logger.warning(
                        "Restarting shared browser runtime for %s after recoverable error: %s",
                        self.config.site,
                        exc,
                    )
                    await self._shutdown_async()
                    await self._restart_async()

            if last_error is not None:
                raise last_error
            raise RuntimeError(f"Unknown browser runtime failure for {self.config.site}")
        except asyncio.CancelledError:
            if not operation_started:
                # Cancellation during launch/recycle cannot be mistaken for an
                # operation that never created resources. Fail closed until
                # its process generation is stopped by the supervisor.
                self.quarantine("Startup or recycle cancelled before resource cleanup was verified")
            raise
        finally:
            with self._lock:
                self._active_tasks = max(0, self._active_tasks - 1)
                self._completed_tasks_since_launch += 1
                self._last_completed_at = time.time()
                if self._quarantined:
                    self._state = "quarantined"
                elif self._closed:
                    self._state = "closed"
                elif self._browser is not None:
                    self._state = "busy" if self._active_tasks > 0 else "ready"
                elif self._state != "restarting":
                    self._state = "idle"

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._launch_async())
        except Exception as exc:
            try:
                loop.run_until_complete(self._shutdown_async())
            except Exception:
                logger.exception(
                    "Failed to clean up browser runtime after startup error: site=%s",
                    self.config.site,
                )
            finally:
                loop.close()
            with self._lock:
                self._startup_error = exc
                self._state = "quarantined" if self._quarantined else "error"
                self._last_error = str(exc)
                if self._loop is loop:
                    self._loop = None
            self._ready_event.set()
            return

        self._ready_event.set()

        try:
            loop.run_forever()
        finally:
            try:
                for task in list(self._tasks):
                    task.cancel()
                if self._tasks:
                    _, pending = loop.run_until_complete(asyncio.wait(self._tasks, timeout=cleanup_timeout_seconds()))
                    if pending:
                        self.quarantine("Runtime operations remain after cancellation")
                loop.run_until_complete(self._shutdown_async())
            except BrowserCleanupUnverified:
                logger.error("Browser cleanup unverified: site=%s", self.config.site)
            finally:
                loop.close()
            with self._lock:
                if self._loop is loop:
                    self._loop = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "site": self.config.site,
                "automation_backend": self.config.automation_backend,
                "channel": self.config.channel,
                "state": self._state,
                "quarantined": self._quarantined,
                "headless": bool(self.config.headless),
                "restart_count": int(self.restart_count),
                "submit_count": int(self.submit_count),
                "max_in_flight_tasks": int(self.config.max_in_flight_tasks),
                "max_tasks_before_restart": int(self.config.max_tasks_before_restart),
                "max_runtime_seconds": float(self.config.max_runtime_seconds),
                "active_tasks": int(self._active_tasks),
                "completed_tasks_since_launch": int(self._completed_tasks_since_launch),
                "thread_alive": bool(self._thread is not None and self._thread.is_alive()),
                "loop_running": bool(self._loop is not None and self._loop.is_running()),
                "browser_ready": self._browser is not None and not self._quarantined,
                "persistent_context": self.config.persistent_user_data_dir is not None,
                "last_started_at": self._last_started_at,
                "last_submit_at": self._last_submit_at,
                "last_completed_at": self._last_completed_at,
                "last_restart_at": self._last_restart_at,
                "last_error": self._last_error,
            }
