"""One monotonic deadline shared by nested fetch and browser operations.

Cooperative checks do not terminate stuck native code. Patrol supervision owns
that hard-stop boundary; this helper prevents nested waits extending its budget.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import time


class OperationDeadlineExceeded(TimeoutError):
    pass


_deadline: ContextVar[float | None] = ContextVar("operation_deadline", default=None)


def current_deadline() -> float | None:
    return _deadline.get()


def remaining_seconds(default: float | None = None) -> float | None:
    """Return the smaller of a caller cap and the remaining shared budget."""
    deadline = current_deadline()
    if deadline is None:
        return default
    remaining = max(0.0, deadline - time.monotonic())
    return remaining if default is None else min(max(0.0, float(default)), remaining)


def check_deadline() -> None:
    if remaining_seconds() == 0:
        raise OperationDeadlineExceeded("Operation deadline exceeded")


@contextmanager
def deadline_scope(deadline: float | None):
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("Deadline must be finite")
    existing = current_deadline()
    resolved = existing if deadline is None else deadline if existing is None else min(existing, deadline)
    token = _deadline.set(resolved)
    try:
        check_deadline()
        yield resolved
    finally:
        _deadline.reset(token)
