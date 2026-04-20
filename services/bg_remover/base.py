"""
Backend-agnostic contract for background-removal providers.

All concrete providers (rembg, remove.bg, Photoroom, ...) accept the
raw source-image bytes and return the processed PNG bytes with a
transparent background. Everything else (storage, auth, UI, etc.) is
implemented on top of this contract so swapping providers in Phase 2
is a one-line config change.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class BackgroundRemovalError(RuntimeError):
    """Raised when a backend fails to process an image."""


class BackgroundRemoverUnavailableError(BackgroundRemovalError):
    """Raised when the backend is not installed / not reachable."""


@runtime_checkable
class BackgroundRemover(Protocol):
    """Minimal interface shared by every background-removal backend."""

    name: str

    def remove_background(self, image_bytes: bytes) -> bytes:
        """Return the processed PNG bytes (transparent background).

        Must raise :class:`BackgroundRemovalError` on a processing failure
        and :class:`BackgroundRemoverUnavailableError` when the backend's
        runtime dependencies are missing.
        """
        ...
