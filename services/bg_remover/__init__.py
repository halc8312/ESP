"""
Background-removal service package.

Exposes a small :mod:`services.bg_remover.base` Protocol so the rest of
the codebase never depends on a specific implementation; Phase 2 can
swap ``rembg`` for a hosted API like remove.bg or Photoroom without
touching route or worker code.
"""
from services.bg_remover.base import (
    BackgroundRemover,
    BackgroundRemovalError,
    BackgroundRemoverUnavailableError,
)
from services.bg_remover.registry import get_bg_remover_backend

__all__ = [
    "BackgroundRemover",
    "BackgroundRemovalError",
    "BackgroundRemoverUnavailableError",
    "get_bg_remover_backend",
]
