"""
Deterministic hashing for translation source text.

The product edit UI shows a "source has changed" badge when the Japanese
title or description is edited after an English translation has been
applied. To support this we store a stable hash of the normalised source
alongside the English field on ``Product``.
"""
from __future__ import annotations

import hashlib
import unicodedata


def normalize_source_for_hash(value: str | None) -> str:
    """Collapse whitespace and NFC-normalise a source string.

    Intentionally does **not** strip HTML. The HTML structure is part of
    the source because changing it constitutes a semantically different
    input to translate.
    """
    if not value:
        return ""
    normalized = unicodedata.normalize("NFC", str(value))
    lines: list[str] = []
    for raw in normalized.splitlines():
        cleaned = " ".join(raw.split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def compute_source_hash(value: str | None) -> str:
    """Return a 64-character SHA-256 digest of the normalised source."""
    normalized = normalize_source_for_hash(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
