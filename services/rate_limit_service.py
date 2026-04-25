"""
Fixed-window rate limiting with Redis/Valkey support and a local-dev fallback.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass

from security_config import is_production_runtime


class RateLimitStoreError(RuntimeError):
    """Raised when the configured shared store is unavailable."""


def _hash_identifier(identifier: str) -> str:
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]


def _key(scope: str, identifier: str) -> str:
    return f"esp:rate:{scope}:{_hash_identifier(identifier)}"


class MemoryRateLimiter:
    def __init__(self):
        self._entries: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def is_limited(self, scope: str, identifier: str, limit: int, window_seconds: int) -> bool:
        key = _key(scope, identifier)
        now = time.time()
        with self._lock:
            count, reset_at = self._entries.get(key, (0, now + window_seconds))
            if reset_at <= now:
                self._entries.pop(key, None)
                return False
            return count >= limit

    def increment(self, scope: str, identifier: str, window_seconds: int) -> int:
        key = _key(scope, identifier)
        now = time.time()
        with self._lock:
            count, reset_at = self._entries.get(key, (0, now + window_seconds))
            if reset_at <= now:
                count, reset_at = 0, now + window_seconds
            count += 1
            self._entries[key] = (count, reset_at)
            return count

    def reset(self, scope: str, identifier: str) -> None:
        with self._lock:
            self._entries.pop(_key(scope, identifier), None)


class RedisRateLimiter:
    def __init__(self, url: str):
        try:
            import redis
        except ImportError as exc:
            raise RateLimitStoreError("redis package is required when REDIS_URL/VALKEY_URL is set.") from exc
        self._client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)

    def is_limited(self, scope: str, identifier: str, limit: int, window_seconds: int) -> bool:
        del window_seconds
        raw_count = self._client.get(_key(scope, identifier))
        return int(raw_count or 0) >= limit

    def increment(self, scope: str, identifier: str, window_seconds: int) -> int:
        key = _key(scope, identifier)
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = pipe.execute()
        return int(count)

    def reset(self, scope: str, identifier: str) -> None:
        self._client.delete(_key(scope, identifier))


@dataclass
class RateLimitDecision:
    allowed: bool
    status_code: int = 200
    message: str = ""


_limiter = None
_limiter_signature: tuple[str | None, bool] | None = None
_limiter_lock = threading.Lock()


def reset_rate_limiter_for_tests() -> None:
    global _limiter, _limiter_signature
    with _limiter_lock:
        _limiter = None
        _limiter_signature = None


def get_rate_limiter():
    global _limiter, _limiter_signature

    url = os.environ.get("REDIS_URL") or os.environ.get("VALKEY_URL")
    production = is_production_runtime()
    signature = (url, production)

    if _limiter is not None and _limiter_signature == signature:
        return _limiter

    with _limiter_lock:
        if _limiter is not None and _limiter_signature == signature:
            return _limiter
        if url:
            _limiter = RedisRateLimiter(url)
        elif production:
            raise RateLimitStoreError("Redis/Valkey rate limit store is required in production.")
        else:
            _limiter = MemoryRateLimiter()
        _limiter_signature = signature
        return _limiter


def get_client_ip(request_obj) -> str:
    forwarded_for = request_obj.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    return request_obj.remote_addr or "unknown"


def is_limited(scope: str, identifier: str, limit: int, window_seconds: int) -> RateLimitDecision:
    try:
        limited = get_rate_limiter().is_limited(scope, identifier, limit, window_seconds)
    except Exception:
        if is_production_runtime():
            return RateLimitDecision(False, 503, "Rate limit service unavailable.")
        raise
    if limited:
        return RateLimitDecision(False, 429, "Too many attempts. Try again later.")
    return RateLimitDecision(True)


def record_attempt(scope: str, identifier: str, window_seconds: int) -> None:
    get_rate_limiter().increment(scope, identifier, window_seconds)


def reset_attempts(scope: str, identifier: str) -> None:
    get_rate_limiter().reset(scope, identifier)
