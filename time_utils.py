"""
UTC time helpers that keep the application's current naive-UTC DB semantics.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

JAPAN_TIMEZONE = ZoneInfo("Asia/Tokyo")

#: Display formats for the Japanese admin screens.
JST_DATETIME_FORMAT = "%Y/%m/%d %H:%M"
JST_DATE_FORMAT = "%Y/%m/%d"


def utc_now() -> datetime:
    """
    Return the current UTC timestamp as a naive datetime.

    The codebase currently stores naive UTC values in SQLAlchemy DateTime
    columns, so we avoid changing persistence semantics while replacing the
    deprecated datetime.utcnow() call site pattern.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def to_jst(value: datetime | None) -> datetime | None:
    """
    Convert a stored timestamp to Japan time.

    Naive values are treated as UTC, matching how the DateTime columns are
    written. Aware values are converted from their own offset.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(JAPAN_TIMEZONE)


def format_jst(value: datetime | None, default: str = "-") -> str:
    """Format a timestamp as `2026/08/03 18:02` in Japan time."""
    localized = to_jst(value)
    if localized is None:
        return default
    return localized.strftime(JST_DATETIME_FORMAT)


def format_jst_date(value: datetime | date | None, default: str = "-") -> str:
    """Format a timestamp as `2026/08/03` in Japan time."""
    if value is None:
        return default
    if isinstance(value, datetime):
        localized = to_jst(value)
        if localized is None:
            return default
        return localized.strftime(JST_DATE_FORMAT)
    return value.strftime(JST_DATE_FORMAT)
