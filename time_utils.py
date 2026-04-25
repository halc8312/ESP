"""
UTC time helpers that keep the application's current naive-UTC DB semantics.
"""
from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """
    Return the current UTC timestamp as a naive datetime.

    The codebase currently stores naive UTC values in SQLAlchemy DateTime
    columns, so we avoid changing persistence semantics while replacing the
    deprecated datetime.utcnow() call site pattern.
    """
    return datetime.now(UTC).replace(tzinfo=None)
