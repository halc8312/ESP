"""
Translator backend factory.

Callers should always go through :func:`get_translator_backend` rather
than importing a specific backend directly. Phase 2 can introduce a
DeepL / OpenAI backend by extending this registry without touching the
rest of the codebase.
"""
from __future__ import annotations

import logging
import os
import threading

from services.translator.base import (
    TranslatorBackend,
    TranslatorUnavailableError,
)


logger = logging.getLogger("services.translator.registry")

_BACKEND_LOCK = threading.Lock()
_BACKEND_INSTANCE: TranslatorBackend | None = None
_BACKEND_NAME_CACHE: str | None = None

#: Runs locally with no account behind it, so it can stand in when a paid
#: backend stops answering.
FALLBACK_BACKEND_NAME = "argos"
_FALLBACK_INSTANCE: TranslatorBackend | None = None


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


def get_fallback_translator_backend() -> TranslatorBackend | None:
    """
    Return the offline backend to use when the configured one cannot serve.

    A paid backend stops working for reasons that have nothing to do with
    the text — an exhausted balance, a revoked key — and when that happens
    every student loses the translate button at once. Argos runs locally and
    its model is baked into the image, so it can carry the work until the
    account is sorted out.

    ``None`` when the configured backend already is the offline one, or when
    the offline one cannot be built either.
    """
    global _FALLBACK_INSTANCE
    if resolve_backend_name() == FALLBACK_BACKEND_NAME:
        return None
    if _FALLBACK_INSTANCE is not None:
        return _FALLBACK_INSTANCE

    with _BACKEND_LOCK:
        if _FALLBACK_INSTANCE is None:
            try:
                _FALLBACK_INSTANCE = _instantiate_backend(FALLBACK_BACKEND_NAME)
            except Exception:
                # The caller only learns that there is no stand-in, and the
                # operator only sees "translation is unavailable" — so why it
                # could not be built has to be written down here or it is lost.
                logger.exception(
                    "translator fallback %r could not be built; "
                    "no stand-in is available for the configured backend",
                    FALLBACK_BACKEND_NAME,
                )
                return None
        return _FALLBACK_INSTANCE


def reset_translator_backend_for_tests() -> None:
    """Clear the cached backend. Intended for test isolation only."""
    global _BACKEND_INSTANCE, _BACKEND_NAME_CACHE, _FALLBACK_INSTANCE
    with _BACKEND_LOCK:
        _BACKEND_INSTANCE = None
        _BACKEND_NAME_CACHE = None
        _FALLBACK_INSTANCE = None
