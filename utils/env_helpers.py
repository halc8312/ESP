from __future__ import annotations

import os
from typing import Any


def parse_bool(value: Any, default: bool = False) -> bool:
    """Return a boolean from an environment-style value."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    return parse_bool(os.environ.get(name), default=default)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default
