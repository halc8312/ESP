"""Lifecycle for the Record City headful browser's private X display."""
from __future__ import annotations

import contextlib
import logging
import os
import selectors
import subprocess
from collections.abc import Iterator


logger = logging.getLogger(__name__)

_PROFILE_ENV = "RECORDCITY_BROWSER_PROFILE"
_START_TIMEOUT_SECONDS = 10.0
_STOP_TIMEOUT_SECONDS = 5.0


def _headful_requested() -> bool:
    provider = str(
        os.environ.get("RECORDCITY_FETCH_PROVIDER", "browser") or "browser"
    ).strip().lower()
    if provider not in {"", "browser", "patchright"}:
        return False
    profile = str(os.environ.get(_PROFILE_ENV, "headless") or "headless")
    return profile.strip().lower() in {"headful", "patchright-headful"}


def _xvfb_command() -> list[str]:
    # ``-displayfd 1`` asks Xvfb to allocate a free display and print its
    # number. TCP is explicitly disabled; the ephemeral Unix socket is visible
    # only inside the worker container. The container runs one unprivileged
    # user, so local access control can be disabled without opening a network
    # listener or adding a persistent credential file.
    return [
        "Xvfb",
        "-displayfd",
        "1",
        "-screen",
        "0",
        "1280x1024x24",
        "-nolisten",
        "tcp",
        "-ac",
    ]


def _stop_xvfb(process) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_STOP_TIMEOUT_SECONDS)
    except Exception as exc:
        # Shutdown must not mask the worker/RQ exception that led us here.
        logger.warning(
            "Record City virtual display cleanup failed: error=%s",
            type(exc).__name__,
        )
    finally:
        stdout = getattr(process, "stdout", None)
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass


def _read_display_number(process) -> str:
    stdout = getattr(process, "stdout", None)
    if stdout is None:
        raise RuntimeError("reason=RC_XVFB_START_FAILED")

    selector = selectors.DefaultSelector()
    try:
        selector.register(stdout, selectors.EVENT_READ)
        if not selector.select(timeout=_START_TIMEOUT_SECONDS):
            raise RuntimeError("reason=RC_XVFB_START_TIMEOUT")
        raw_number = str(stdout.readline() or "").strip()
    finally:
        selector.close()

    if process.poll() is not None or not raw_number.isdigit():
        raise RuntimeError("reason=RC_XVFB_START_FAILED")
    number = int(raw_number)
    if not 0 <= number <= 65535:
        raise RuntimeError("reason=RC_XVFB_START_FAILED")
    return f":{number}"


def _start_xvfb():
    try:
        process = subprocess.Popen(
            _xvfb_command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"reason=RC_XVFB_START_FAILED,error={type(exc).__name__}"
        ) from None

    try:
        display = _read_display_number(process)
    except Exception:
        _stop_xvfb(process)
        raise
    return process, display


@contextlib.contextmanager
def recordcity_virtual_display() -> Iterator[None]:
    """Run one private Xvfb for a headful Record City worker lifecycle.

    Existing displays are respected for local development and the diagnostic
    CLI. The Render worker starts this before browser warm-up and keeps it
    alive until ``run_worker`` has returned and closed the browser pool.
    """
    if not _headful_requested() or str(os.environ.get("DISPLAY") or "").strip():
        yield
        return

    process, display = _start_xvfb()
    previous_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = display
    logger.info("Record City private virtual display started: display=%s", display)
    try:
        yield
    finally:
        if previous_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previous_display
        _stop_xvfb(process)
        logger.info("Record City private virtual display stopped")
