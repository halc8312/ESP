"""
Worker-callable background-removal task execution.

A bg-removal job:

1. Loads the ``ImageProcessingJob`` row identified by ``job_id`` and
   flips its status to ``running``.
2. Fetches the source image bytes (either from the public ``/media/``
   prefix served by the web service or from an external ``http(s)``
   URL captured when the product was scraped).
3. Runs the configured :class:`BackgroundRemover` backend on the bytes.
4. POSTs the processed PNG bytes to the web service's internal
   ``/internal/bg-removal/<job_id>/upload`` endpoint (authenticated
   with the shared HMAC secret), which persists the file and calls
   ``mark_succeeded``.

The ``Product`` row is never updated by the worker; the operator
explicitly applies or rejects the result from the review UI.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urljoin

import requests

from services.bg_remover import get_bg_remover_backend
from services.bg_remover.base import BackgroundRemovalError
from services.bg_remover.internal_auth import (
    JOB_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    compute_signature,
)
from services.bg_remover.job_store import (
    get_job_by_job_id,
    mark_failed,
    mark_running,
)


logger = logging.getLogger("jobs.bg_removal_tasks")


DEFAULT_SOURCE_FETCH_TIMEOUT_SECONDS = 30
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 60


def _resolve_web_base_url() -> str:
    """Return the base URL the worker should use to reach esp-web."""
    configured = (
        os.environ.get("ESP_WEB_INTERNAL_URL")
        or os.environ.get("WEB_INTERNAL_URL")
        or os.environ.get("WEB_PUBLIC_URL")
        or ""
    ).strip()
    if configured:
        return configured.rstrip("/")

    # Render's private network explicitly blocks port 10000 (the public
    # HTTPS port) and 18012/18013/19099 for internal communication, so the
    # esp-web service must listen on a *second* port for worker traffic.
    # The default matches render.yaml's ``INTERNAL_PORT=8080``; operators
    # should still set ``WEB_INTERNAL_URL`` explicitly when deploying.
    return "http://esp-web:8080"


def _resolve_source_fetch_timeout() -> int:
    raw = os.environ.get("BG_REMOVAL_SOURCE_FETCH_TIMEOUT_SECONDS")
    try:
        return max(5, int(raw)) if raw else DEFAULT_SOURCE_FETCH_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_SOURCE_FETCH_TIMEOUT_SECONDS


def _resolve_upload_timeout() -> int:
    raw = os.environ.get("BG_REMOVAL_UPLOAD_TIMEOUT_SECONDS")
    try:
        return max(5, int(raw)) if raw else DEFAULT_UPLOAD_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_TIMEOUT_SECONDS


def _resolve_source_url(source_image_url: str) -> str:
    """Turn a stored source image URL into something ``requests`` can GET."""
    if source_image_url.startswith(("http://", "https://")):
        return source_image_url

    if source_image_url.startswith("/"):
        base = _resolve_web_base_url()
        return urljoin(base + "/", source_image_url.lstrip("/"))

    raise BackgroundRemovalError(
        f"unsupported source_image_url scheme: {source_image_url!r}"
    )


def _fetch_source_bytes(source_image_url: str) -> bytes:
    resolved = _resolve_source_url(source_image_url)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; esp-worker/1.0)",
    }
    try:
        response = requests.get(
            resolved,
            timeout=_resolve_source_fetch_timeout(),
            headers=headers,
            stream=True,
        )
        response.raise_for_status()
        data = response.content
    except requests.RequestException as exc:
        raise BackgroundRemovalError(
            f"failed to download source image: {exc}"
        ) from exc

    if not data:
        raise BackgroundRemovalError("source image fetch returned empty body")
    return data


def _upload_result_bytes(
    *,
    job_id: str,
    result_bytes: bytes,
) -> dict[str, Any]:
    base = _resolve_web_base_url()
    url = f"{base}/internal/bg-removal/{job_id}/upload"

    timestamp = str(int(time.time()))
    signature = compute_signature(
        job_id=job_id, timestamp=timestamp, body=result_bytes
    )
    headers = {
        "Content-Type": "application/octet-stream",
        SIGNATURE_HEADER: signature,
        TIMESTAMP_HEADER: timestamp,
        JOB_ID_HEADER: job_id,
    }

    try:
        response = requests.post(
            url,
            data=result_bytes,
            headers=headers,
            timeout=_resolve_upload_timeout(),
        )
    except requests.RequestException as exc:
        raise BackgroundRemovalError(
            f"failed to reach internal upload endpoint: {exc}"
        ) from exc

    if response.status_code >= 400:
        raise BackgroundRemovalError(
            "internal upload endpoint returned "
            f"{response.status_code}: {response.text[:300]}"
        )

    try:
        return response.json() if response.content else {}
    except ValueError:
        return {}


def execute_bg_removal_job(job_id: str) -> dict[str, Any]:
    """Worker entrypoint — runs a bg-removal job end-to-end."""
    job = get_job_by_job_id(job_id)
    if job is None:
        logger.warning("bg-removal job row missing for %s", job_id)
        return {"job_id": job_id, "status": "missing"}

    if job.status not in {"queued", "running"}:
        logger.info(
            "bg-removal job %s already in state %s; skipping",
            job_id,
            job.status,
        )
        return {"job_id": job_id, "status": job.status}

    source_image_url = job.source_image_url
    mark_running(job_id)

    try:
        source_bytes = _fetch_source_bytes(source_image_url)
        backend = get_bg_remover_backend()
        result_bytes = backend.remove_background(source_bytes)
        if not result_bytes:
            raise BackgroundRemovalError("backend returned empty bytes")

        upload_response = _upload_result_bytes(
            job_id=job_id, result_bytes=result_bytes
        )
    except BackgroundRemovalError as exc:
        logger.exception("bg-removal job %s failed", job_id)
        mark_failed(job_id, error_message=str(exc))
        raise
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.exception("bg-removal job %s failed unexpectedly", job_id)
        mark_failed(job_id, error_message=f"{type(exc).__name__}: {exc}")
        raise

    return {
        "job_id": job_id,
        "status": "succeeded",
        "result_image_url": upload_response.get("result_image_url"),
    }
