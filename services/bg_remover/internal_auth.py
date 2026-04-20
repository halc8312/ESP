"""
HMAC helpers for the worker -> web internal upload channel.

Because Render can only attach a persistent disk to a single service,
the media worker cannot write directly to ``/var/data/images`` on the
web service. Instead the worker POSTs the processed PNG bytes to an
internal endpoint on ``esp-web`` (``/internal/bg-removal/...``) and
authenticates the request with a shared-secret HMAC signature rather
than a browser session cookie.

The shared secret is read from ``BG_REMOVAL_INTERNAL_SECRET`` (and
falls back to ``SECRET_KEY`` so the wiring still works when operators
forget to set the dedicated env var; the combined ``SECRET_KEY`` is
already a cross-service invariant per AGENTS.md).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional


SIGNATURE_HEADER = "X-ESP-BG-Signature"
TIMESTAMP_HEADER = "X-ESP-BG-Timestamp"
JOB_ID_HEADER = "X-ESP-BG-Job-Id"

DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300


def _resolve_secret() -> str:
    """Return the shared secret string used to sign internal uploads."""
    explicit = (os.environ.get("BG_REMOVAL_INTERNAL_SECRET") or "").strip()
    if explicit:
        return explicit

    fallback = (os.environ.get("SECRET_KEY") or "").strip()
    if fallback:
        return fallback

    # Development fallback: keep tests + local dev running without mandating
    # a secret. Production cross-checks the real SECRET_KEY so this string
    # never gates real data.
    return "esp-dev-insecure-internal-bg-secret"


def _canonical_payload(*, job_id: str, timestamp: str, body: bytes) -> bytes:
    body_digest = hashlib.sha256(body or b"").hexdigest()
    return f"{job_id}.{timestamp}.{body_digest}".encode("utf-8")


def compute_signature(*, job_id: str, timestamp: str, body: bytes) -> str:
    secret = _resolve_secret().encode("utf-8")
    payload = _canonical_payload(job_id=job_id, timestamp=timestamp, body=body)
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    job_id: Optional[str],
    timestamp: Optional[str],
    body: bytes,
    signature: Optional[str],
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
) -> bool:
    if not job_id or not timestamp or not signature:
        return False

    try:
        ts_value = int(timestamp)
    except (TypeError, ValueError):
        return False

    import time

    now = int(time.time())
    if abs(now - ts_value) > max_clock_skew_seconds:
        return False

    expected = compute_signature(job_id=job_id, timestamp=timestamp, body=body)
    return hmac.compare_digest(expected, signature)
