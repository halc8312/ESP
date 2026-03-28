"""
Shared Playwright browser runtime for worker-side browser reuse.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from playwright.async_api import async_playwright


logger = logging.getLogger("browser_runtime")


@dataclass(frozen=True)
class BrowserRuntimeConfig:
    site: str
    browser_name: str = "chromium"
    headless: bool = True
    launch_args: tuple[str, ...] = ()
    startup_timeout_seconds: float = 60.0
    restart_attempts: int = 1
    max_in_flight_tasks: int = 1
    max_tasks_before_restart: int = 0
    max_runtime_seconds: float = 0.0


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
        self._browser = None
        self._startup_error: BaseException | None = None
        self._closed = False
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
            if self._closed:
                raise RuntimeError(f"Browser runtime for {self.config.site} is closed")
            if self._thread is not None and self._thread.is_alive():
                return

            self._ready_event.clear()
            self._startup_error = None
            self._state = "starting"
            self._thread = threading.Thread(
                target=self._thread_main,
                name=f"browser-runtime-{self.config.site}",
                daemon=True,
            )
            self._thread.start()

        if not self._ready_event.wait(timeout=self.config.startup_timeout_seconds):
            with self._lock:
                self._state = "error"
                self._last_error = f"Timed out starting browser runtime for {self.config.site}"
            raise TimeoutError(f"Timed out starting browser runtime for {self.config.site}")
        if self._startup_error is not None:
            with self._lock:
                self._state = "error"
                self._last_error = str(self._startup_error)
            raise RuntimeError(f"Failed to start browser runtime for {self.config.site}") from self._startup_error

    def submit(self, coro_factory: Callable[[Any], Awaitable[Any]]) -> Future:
        self.start()
        if self._loop is None:
            raise RuntimeError(f"Browser runtime for {self.config.site} has no event loop")
        acquired = self._submit_permit.acquire(timeout=self.config.startup_timeout_seconds)
        if not acquired:
            raise TimeoutError(f"Timed out waiting for browser runtime slot for {self.config.site}")

        with self._lock:
            self.submit_count += 1
            self._last_submit_at = time.time()
            self._state = "busy"

        try:
            future = asyncio.run_coroutine_threadsafe(self._run_with_restart(coro_factory), self._loop)
        except Exception:
            self._submit_permit.release()
            raise

        def _release_permit(_future: Future) -> None:
            try:
                self._submit_permit.release()
            except ValueError:
                pass
            with self._lock:
                if self._closed:
                    self._state = "closed"
                elif self._browser is not None:
                    self._state = "ready"
                else:
                    self._state = "idle"

        future.add_done_callback(_release_permit)
        return future

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._state = "closing"
            loop = self._loop
            thread = self._thread

        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)

        with self._lock:
            self._thread = None
            self._loop = None
            self._state = "closed"

    async def _launch_async(self) -> None:
        self._playwright = await async_playwright().start()
        browser_type = getattr(self._playwright, self.config.browser_name)
        self._browser = await browser_type.launch(
            headless=self.config.headless,
            args=list(self.config.launch_args),
        )
        with self._lock:
            self._state = "ready"
            self._last_started_at = time.time()
            self._last_error = None
            self._completed_tasks_since_launch = 0

    async def _shutdown_async(self) -> None:
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None

        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        with self._lock:
            if not self._closed and self._state != "restarting":
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
                    return await coro_factory(self._browser)
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
        finally:
            with self._lock:
                self._active_tasks = max(0, self._active_tasks - 1)
                self._completed_tasks_since_launch += 1
                self._last_completed_at = time.time()
                if self._closed:
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
            with self._lock:
                self._startup_error = exc
                self._state = "error"
                self._last_error = str(exc)
            self._ready_event.set()
            loop.close()
            return

        self._ready_event.set()

        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(self._shutdown_async())
            loop.close()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "site": self.config.site,
                "state": self._state,
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
                "browser_ready": self._browser is not None,
                "last_started_at": self._last_started_at,
                "last_submit_at": self._last_submit_at,
                "last_completed_at": self._last_completed_at,
                "last_restart_at": self._last_restart_at,
                "last_error": self._last_error,
            }
