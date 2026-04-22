"""
Translator backend factory.

Callers should always go through :func:`get_translator_backend` rather
than importing a specific backend directly. Phase 2 can introduce a
DeepL / OpenAI backend by extending this registry without touching the
rest of the codebase.
"""
from __future__ import annotations

import os
import threading

from services.translator.base import (
    TranslatorBackend,
    TranslatorUnavailableError,
)


_BACKEND_LOCK = threading.Lock()
_BACKEND_INSTANCE: TranslatorBackend | None = None
_BACKEND_NAME_CACHE: str | None = None


def resolve_backend_name() -> str:
    """Return the configured backend name (lower-case, stripped)."""
    raw = os.environ.get("TRANSLATOR_BACKEND", "argos")
    return str(raw or "argos").strip().lower() or "argos"


def _instantiate_backend(name: str) -> TranslatorBackend:
    source_language = os.environ.get("TRANSLATOR_SOURCE_LANG", "ja")
    target_language = os.environ.get("TRANSLATOR_TARGET_LANG", "en")

    if name == "argos":
        from services.translator.argos_backend import ArgosTranslatorBackend

        return ArgosTranslatorBackend(
            source_language=source_language,
            target_language=target_language,
        )

    if name == "openai":
        from services.translator.openai_backend import OpenAITranslatorBackend

        return OpenAITranslatorBackend(
            source_language=source_language,
            target_language=target_language,
        )

    raise TranslatorUnavailableError(
        f"Unsupported translator backend: {name!r}"
    )


def get_translator_backend() -> TranslatorBackend:
    """Return a process-wide singleton backend instance.

    Keeping the backend as a singleton lets the Argos model load once per
    worker process and be reused across jobs. The lock guards against
    concurrent first-use in a multi-threaded worker.
    """
    global _BACKEND_INSTANCE, _BACKEND_NAME_CACHE
    desired_name = resolve_backend_name()
    if _BACKEND_INSTANCE is not None and _BACKEND_NAME_CACHE == desired_name:
        return _BACKEND_INSTANCE

    with _BACKEND_LOCK:
        if _BACKEND_INSTANCE is None or _BACKEND_NAME_CACHE != desired_name:
            _BACKEND_INSTANCE = _instantiate_backend(desired_name)
            _BACKEND_NAME_CACHE = desired_name
        return _BACKEND_INSTANCE


def reset_translator_backend_for_tests() -> None:
    """Clear the cached backend. Intended for test isolation only."""
    global _BACKEND_INSTANCE, _BACKEND_NAME_CACHE
    with _BACKEND_LOCK:
        _BACKEND_INSTANCE = None
        _BACKEND_NAME_CACHE = None
