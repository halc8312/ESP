"""
Small reusable runner for detail-field extraction strategies.

Each strategy can be a precomputed value or a lazy callable so parsers can keep
their current extraction order without eagerly computing every fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


@dataclass(frozen=True)
class DetailFieldStrategy:
    source: str
    value: Any = None
    resolver: Callable[[], Any] | None = None

    def resolve(self) -> Any:
        if callable(self.resolver):
            return self.resolver()
        return self.value


def run_detail_field_strategies(
    *strategies: DetailFieldStrategy,
    validator: Callable[[Any], bool] | None = None,
    default: Any = None,
) -> tuple[Any, str]:
    effective_validator = validator or _has_value
    for strategy in strategies:
        value = strategy.resolve()
        if effective_validator(value):
            return value, strategy.source
    return default, ""
