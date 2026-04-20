"""
Translator backend protocol and shared error types.

All translators implement the same narrow interface so the job handler
and routes stay backend-agnostic. Backends are expected to be:

* **idempotent** — calling ``translate_plain`` twice with identical input
  should return the same string.
* **thread-safe** — a single backend instance may be shared across worker
  threads. Argos/ctranslate2 are safe in this regard.
* **lazy** — expensive resources (ML models) should be loaded on first
  use, not at construction time, so importing the backend is cheap.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class TranslationError(RuntimeError):
    """Raised when a backend fails to translate a given input."""


class TranslatorUnavailableError(TranslationError):
    """Raised when a backend cannot be initialised (e.g. missing model)."""


@runtime_checkable
class TranslatorBackend(Protocol):
    """Narrow translator contract used by the media worker."""

    name: str
    source_language: str
    target_language: str

    def translate_plain(self, text: str) -> str:
        """Translate a single plain-text string."""

    def translate_html(self, html: str) -> str:
        """Translate text content inside an HTML fragment.

        Implementations must preserve the tag / attribute structure of the
        input and only rewrite user-visible text nodes. This keeps Shopify
        output safe to copy/paste.
        """
