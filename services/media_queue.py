"""
Media job queue helper.

The existing :mod:`services.queue_backend` is deeply tied to the scrape job
lifecycle (scrape_jobs table, selector repairs, backlog reconciliation).
Translation and background-removal jobs have a different lifecycle — their
status is tracked on their own domain tables such as
``translation_suggestions`` — so they enqueue through this thinner helper
instead.

The helper also implements Phase 1's **logical queue separation**:

* ``SCRAPE_QUEUE_NAME`` (default: ``scrape``) — used by scrape jobs.
* ``MEDIA_QUEUE_NAME`` (default: same as scrape) — used by media jobs.

In Phase 1 both names default to the same value, so a single worker
process drains both, and the scheme is a no-op for operators. In Phase 2
the operator sets ``MEDIA_QUEUE_NAME=media`` and spins up an
``esp-worker-media`` service listening to only that queue; no code
change is needed.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from flask import current_app, has_app_context


DEFAULT_MEDIA_JOB_TIMEOUT_SECONDS = 1800
DEFAULT_MEDIA_JOB_RESULT_TTL_SECONDS = 0
DEFAULT_MEDIA_JOB_FAILURE_TTL_SECONDS = 604800


def _read_config_value(key: str, default: str) -> str:
    if has_app_context():
        configured = current_app.config.get(key)
        if configured:
            return str(configured).strip()
    return str(os.environ.get(key, default) or default).strip()


def _read_int_config(key: str, default: int) -> int:
    raw = _read_config_value(key, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def resolve_scrape_queue_name() -> str:
    return _read_config_value("SCRAPE_QUEUE_NAME", "scrape") or "scrape"


def resolve_media_queue_name() -> str:
    """Return the queue name to use for media jobs.

    Falls back to the scrape queue name so Phase 1 deployments work
    without any env changes.
    """
    configured = _read_config_value("MEDIA_QUEUE_NAME", "")
    return configured or resolve_scrape_queue_name()


def resolve_worker_queue_names() -> list[str]:
    """Return the deduplicated list of queues a combined worker should listen to."""
    queue_names: list[str] = []
    for name in (resolve_scrape_queue_name(), resolve_media_queue_name()):
        if name and name not in queue_names:
            queue_names.append(name)
    return queue_names


def resolve_queue_backend_name() -> str:
    return (
        _read_config_value("SCRAPE_QUEUE_BACKEND", "inmemory").lower()
        or "inmemory"
    )


def resolve_redis_url() -> str:
    return _read_config_value("REDIS_URL", "redis://localhost:6379/0")


def enqueue_media_job(
    *,
    job_id: str,
    func: str,
    args: tuple[Any, ...] = (),
    kwargs: Optional[dict[str, Any]] = None,
    description: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    result_ttl_seconds: Optional[int] = None,
    failure_ttl_seconds: Optional[int] = None,
) -> str:
    """Enqueue a job onto the media queue and return its job id.

    Only the ``rq`` backend is supported for media jobs; when running
    with the in-memory backend (e.g. during tests or local dev) the
    caller is expected to execute the job synchronously instead.
    """
    backend_name = resolve_queue_backend_name()
    if backend_name != "rq":
        raise RuntimeError(
            "Media jobs require SCRAPE_QUEUE_BACKEND=rq; configure Redis "
            "or run the job synchronously in-process for local/testing."
        )

    from redis import Redis

    from services.rq_compat import import_rq_queue

    Queue = import_rq_queue()
    connection = Redis.from_url(resolve_redis_url())
    queue = Queue(
        resolve_media_queue_name(),
        connection=connection,
        default_timeout=timeout_seconds
        if timeout_seconds is not None
        else _read_int_config(
            "MEDIA_JOB_TIMEOUT_SECONDS", DEFAULT_MEDIA_JOB_TIMEOUT_SECONDS
        ),
    )

    queue.enqueue_call(
        func=func,
        args=args,
        kwargs=kwargs or {},
        job_id=job_id,
        description=description or func,
        result_ttl=result_ttl_seconds
        if result_ttl_seconds is not None
        else _read_int_config(
            "MEDIA_JOB_RESULT_TTL_SECONDS",
            DEFAULT_MEDIA_JOB_RESULT_TTL_SECONDS,
        ),
        failure_ttl=failure_ttl_seconds
        if failure_ttl_seconds is not None
        else _read_int_config(
            "MEDIA_JOB_FAILURE_TTL_SECONDS",
            DEFAULT_MEDIA_JOB_FAILURE_TTL_SECONDS,
        ),
    )
    return job_id
