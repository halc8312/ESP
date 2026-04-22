"""
Background-removal backend factory.

Callers should always go through :func:`get_bg_remover_backend` rather
than importing a specific backend directly. Phase 2 can swap ``rembg``
for a hosted API (remove.bg, Photoroom, Bria, ...) by extending this
registry without touching the routes or worker.
"""
from __future__ import annotations

import os
import threading

from services.bg_remover.base import (
    BackgroundRemover,
    BackgroundRemoverUnavailableError,
)


_BACKEND_LOCK = threading.Lock()
_BACKEND_INSTANCE: BackgroundRemover | None = None
_BACKEND_NAME_CACHE: str | None = None
_BACKEND_PINNED: bool = False


def resolve_backend_name() -> str:
    """Return the configured backend name (lower-case, stripped)."""
    raw = os.environ.get("BG_REMOVAL_BACKEND", "rembg")
    return str(raw or "rembg").strip().lower() or "rembg"


def _instantiate_backend(name: str) -> BackgroundRemover:
    if name == "rembg":
        from services.bg_remover.rembg_backend import RembgBackgroundRemover

        return RembgBackgroundRemover()

    raise BackgroundRemoverUnavailableError(
        f"Unsupported bg-removal backend: {name!r}"
    )


def get_bg_remover_backend() -> BackgroundRemover:
    """Return a process-wide singleton backend instance.

    Keeping the backend as a singleton lets the rembg ONNX session load
    once per worker process and be reused across jobs. The lock guards
    against concurrent first-use in a multi-threaded worker.
    """
    global _BACKEND_INSTANCE, _BACKEND_NAME_CACHE
    if _BACKEND_PINNED and _BACKEND_INSTANCE is not None:
        return _BACKEND_INSTANCE

    desired_name = resolve_backend_name()
    if _BACKEND_INSTANCE is not None and _BACKEND_NAME_CACHE == desired_name:
        return _BACKEND_INSTANCE

    with _BACKEND_LOCK:
        if _BACKEND_PINNED and _BACKEND_INSTANCE is not None:
            return _BACKEND_INSTANCE
        if _BACKEND_INSTANCE is None or _BACKEND_NAME_CACHE != desired_name:
            _BACKEND_INSTANCE = _instantiate_backend(desired_name)
            _BACKEND_NAME_CACHE = desired_name
        return _BACKEND_INSTANCE


def set_bg_remover_backend_for_tests(backend: BackgroundRemover | None) -> None:
    """Install (or clear) a specific backend instance for tests.

    When ``backend`` is not ``None`` the registry becomes "pinned" and
    :func:`get_bg_remover_backend` will return it regardless of the
    ``BG_REMOVAL_BACKEND`` env var, which is what test fixtures want.
    """
    global _BACKEND_INSTANCE, _BACKEND_NAME_CACHE, _BACKEND_PINNED
    with _BACKEND_LOCK:
        _BACKEND_INSTANCE = backend
        _BACKEND_NAME_CACHE = getattr(backend, "name", None) if backend else None
        _BACKEND_PINNED = backend is not None


def reset_bg_remover_backend_for_tests() -> None:
    """Clear the cached backend. Intended for test isolation only."""
    set_bg_remover_backend_for_tests(None)
