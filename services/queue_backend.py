"""
Queue backend selection and scrape job contract helpers.
"""
from __future__ import annotations

import os
import uuid
from functools import lru_cache
from typing import Any, Optional, Protocol, runtime_checkable

from flask import current_app, has_app_context, url_for


SCRAPE_JOB_TERMINAL_STATUSES = frozenset({"completed", "failed"})
SCRAPE_JOB_DEFAULT_RETENTION_SECONDS = 3600


def derive_scrape_job_mode(job_payload: dict[str, Any]) -> str:
    context = job_payload.get("context") or {}
    mode = context.get("mode")
    if mode:
        return str(mode)

    persist_to_db = context.get("persist_to_db")
    if persist_to_db is None and isinstance(job_payload.get("result"), dict):
        persist_to_db = job_payload["result"].get("persist_to_db")

    return "preview" if persist_to_db is False else "persist"


def build_scrape_job_result_summary(job_payload: dict[str, Any]) -> Optional[dict[str, int]]:
    result = job_payload.get("result")
    if result is None:
        return None

    if isinstance(result, dict):
        items = result.get("items") or []
        return {
            "items_count": len(items),
            "excluded_count": result.get("excluded_count", 0),
            "new_count": result.get("new_count", 0),
            "updated_count": result.get("updated_count", 0),
        }

    if isinstance(result, list):
        return {
            "items_count": len(result),
            "excluded_count": 0,
            "new_count": 0,
            "updated_count": 0,
        }

    return None


def build_scrape_job_error_payload(job_payload: dict[str, Any]) -> Optional[dict[str, str]]:
    error_payload = job_payload.get("error_payload")
    if isinstance(error_payload, dict) and error_payload.get("message"):
        return {
            "message": str(error_payload["message"]),
            "kind": str(error_payload.get("kind") or "job_failed"),
        }
    error = job_payload.get("error")
    if not error:
        return None
    return {
        "message": str(error),
        "kind": "job_failed",
    }


def build_scrape_job_result_url(job_payload: dict[str, Any]) -> str:
    job_id = job_payload["job_id"]
    if derive_scrape_job_mode(job_payload) == "preview":
        return url_for("scrape.scrape_form", job_id=job_id)
    return url_for("scrape.scrape_result", job_id=job_id)


def normalize_scrape_job_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "job_id": job_payload.get("job_id"),
        "site": job_payload.get("site"),
        "status": job_payload.get("status"),
        "result": job_payload.get("result"),
        "error": job_payload.get("error"),
        "elapsed_seconds": job_payload.get("elapsed_seconds"),
        "queue_position": job_payload.get("queue_position"),
        "context": job_payload.get("context") or {},
        "created_at": job_payload.get("created_at"),
        "started_at": job_payload.get("started_at"),
        "finished_at": job_payload.get("finished_at"),
        "updated_at": job_payload.get("updated_at"),
        "error_payload": job_payload.get("error_payload"),
    }
    normalized["mode"] = derive_scrape_job_mode(normalized)
    normalized["error_payload"] = build_scrape_job_error_payload(normalized)
    normalized["result_summary"] = build_scrape_job_result_summary(normalized)
    return normalized


def serialize_scrape_job_for_api(job_payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_scrape_job_payload(job_payload)
    return {
        "job_id": normalized["job_id"],
        "site": normalized["site"],
        "status": normalized["status"],
        "result": normalized["result"],
        "error": normalized["error"],
        "elapsed_seconds": normalized["elapsed_seconds"],
        "queue_position": normalized["queue_position"],
        "context": normalized["context"],
        "created_at": normalized["created_at"],
        "finished_at": normalized["finished_at"],
        "result_url": build_scrape_job_result_url(normalized),
        "result_summary": normalized["result_summary"],
    }


@runtime_checkable
class QueueBackend(Protocol):
    def enqueue(
        self,
        site: str,
        task_fn,
        task_args: tuple = (),
        task_kwargs: dict | None = None,
        user_id: int | None = None,
        context: Optional[dict] = None,
        request_payload: Optional[dict] = None,
        mode: str | None = None,
    ) -> str:
        ...

    def get_status(self, job_id: str, user_id: int | None = None) -> Optional[dict[str, Any]]:
        ...

    def get_jobs_for_user(
        self,
        user_id: int,
        limit: int = 5,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        ...


class InMemoryQueueBackend:
    def __init__(self) -> None:
        from services.scrape_queue import get_queue

        self._get_queue = get_queue

    def _build_instrumented_task(self, job_id: str, task_fn):
        from services.scrape_job_runtime import run_tracked_job

        def instrumented_task(*task_args, **task_kwargs):
            return run_tracked_job(job_id, task_fn, *task_args, **task_kwargs)

        return instrumented_task

    def enqueue(
        self,
        site: str,
        task_fn,
        task_args: tuple = (),
        task_kwargs: dict | None = None,
        user_id: int | None = None,
        context: Optional[dict] = None,
        request_payload: Optional[dict] = None,
        mode: str | None = None,
    ) -> str:
        from services.scrape_job_store import create_job_record, get_job_record, mark_job_failed

        job_id = str(uuid.uuid4())
        create_job_record(
            job_id=job_id,
            site=site,
            user_id=user_id,
            context=context,
            request_payload=request_payload,
            mode=mode,
        )
        try:
            self._get_queue().enqueue(
                site=site,
                task_fn=self._build_instrumented_task(job_id, task_fn),
                task_args=task_args,
                task_kwargs=task_kwargs,
                user_id=user_id,
                context=context,
                job_id=job_id,
            )
        except Exception as exc:
            mark_job_failed(job_id, str(exc))
            raise

        live_status = self._get_queue().get_status(job_id, user_id=user_id)
        stored_status = get_job_record(job_id, user_id=user_id)
        return (live_status or stored_status or {}).get("job_id", job_id)

    def get_status(self, job_id: str, user_id: int | None = None) -> Optional[dict[str, Any]]:
        from services.scrape_job_store import get_job_record, maybe_mark_job_stalled

        maybe_mark_job_stalled(job_id)

        live_status = self._get_queue().get_status(job_id, user_id=user_id)
        stored_status = get_job_record(job_id, user_id=user_id)
        return _merge_job_payload(stored_status, live_status)

    def get_jobs_for_user(
        self,
        user_id: int,
        limit: int = 5,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        from services.scrape_job_store import list_job_records_for_user, maybe_mark_job_stalled

        live_jobs = self._get_queue().get_jobs_for_user(
            user_id=user_id,
            limit=limit,
            include_terminal=include_terminal,
        )
        stored_jobs = list_job_records_for_user(
            user_id=user_id,
            limit=limit,
            include_terminal=include_terminal,
        )
        for stored_job in stored_jobs:
            maybe_mark_job_stalled(stored_job["job_id"])
        stored_jobs = list_job_records_for_user(
            user_id=user_id,
            limit=limit,
            include_terminal=include_terminal,
        )

        merged_jobs: dict[str, dict[str, Any]] = {}
        for stored_job in stored_jobs:
            merged_jobs[stored_job["job_id"]] = normalize_scrape_job_payload(stored_job)
        for live_job in live_jobs:
            merged_jobs[live_job["job_id"]] = _merge_job_payload(
                merged_jobs.get(live_job["job_id"]),
                live_job,
            )

        sorted_jobs = sorted(
            merged_jobs.values(),
            key=lambda job: _job_sort_key(job.get("created_at")),
            reverse=True,
        )
        return sorted_jobs[: max(1, int(limit or 5))]


class RQQueueBackend:
    def __init__(self, redis_url: str, queue_name: str) -> None:
        self._redis_url = redis_url
        self._queue_name = queue_name

    def _get_rq_queue(self):
        try:
            from redis import Redis
            from services.rq_compat import import_rq_queue
        except ImportError as exc:
            raise RuntimeError("RQ backend requires `rq` and `redis` packages") from exc

        Queue = import_rq_queue()
        return Queue(
            self._queue_name,
            connection=Redis.from_url(self._redis_url),
            default_timeout=int(os.environ.get("SCRAPE_JOB_TIMEOUT_SECONDS", "1800")),
        )

    def _rq_job_exists(self, job_id: str) -> bool:
        try:
            from redis import Redis
            from services.rq_compat import import_rq_job, import_rq_no_such_job_error
        except ImportError as exc:
            raise RuntimeError("RQ backend requires `rq` and `redis` packages") from exc

        Job = import_rq_job()
        NoSuchJobError = import_rq_no_such_job_error()
        connection = Redis.from_url(self._redis_url)
        try:
            Job.fetch(job_id, connection=connection)
            return True
        except NoSuchJobError:
            return False

    def _maybe_mark_missing_job_as_orphaned(
        self,
        job_payload: Optional[dict[str, Any]],
        *,
        user_id: int | None = None,
    ) -> Optional[dict[str, Any]]:
        if not job_payload or _is_terminal_status(job_payload.get("status")):
            return job_payload

        try:
            job_exists = self._rq_job_exists(str(job_payload["job_id"]))
        except Exception:
            return job_payload

        if job_exists:
            return job_payload

        from services.scrape_job_store import get_job_record, maybe_mark_job_orphaned

        if maybe_mark_job_orphaned(str(job_payload["job_id"])):
            refreshed = get_job_record(str(job_payload["job_id"]), user_id=user_id)
            if refreshed is not None:
                return refreshed
        return job_payload

    def enqueue(
        self,
        site: str,
        task_fn,
        task_args: tuple = (),
        task_kwargs: dict | None = None,
        user_id: int | None = None,
        context: Optional[dict] = None,
        request_payload: Optional[dict] = None,
        mode: str | None = None,
    ) -> str:
        from services.scrape_job_store import create_job_record, mark_job_failed

        if request_payload is None:
            raise ValueError("RQ backend requires request_payload for scrape jobs")

        job_id = str(uuid.uuid4())
        create_job_record(
            job_id=job_id,
            site=site,
            user_id=user_id,
            context=context,
            request_payload=request_payload,
            mode=mode,
        )

        try:
            self._get_rq_queue().enqueue_call(
                func="jobs.scrape_tasks.run_enqueued_scrape_job",
                args=(job_id, request_payload),
                job_id=job_id,
                description=f"scrape:{site}:{job_id}",
                result_ttl=0,
                failure_ttl=int(os.environ.get("SCRAPE_JOB_FAILURE_TTL_SECONDS", "604800")),
            )
        except Exception as exc:
            mark_job_failed(job_id, str(exc))
            raise

        return job_id

    def get_status(self, job_id: str, user_id: int | None = None) -> Optional[dict[str, Any]]:
        from services.scrape_job_store import get_job_record, maybe_mark_job_stalled

        maybe_mark_job_stalled(job_id)
        stored = get_job_record(job_id, user_id=user_id)
        return self._maybe_mark_missing_job_as_orphaned(stored, user_id=user_id)

    def get_jobs_for_user(
        self,
        user_id: int,
        limit: int = 5,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        from services.scrape_job_store import list_job_records_for_user, maybe_mark_job_stalled

        jobs = list_job_records_for_user(
            user_id=user_id,
            limit=limit,
            include_terminal=include_terminal,
        )
        for job in jobs:
            maybe_mark_job_stalled(job["job_id"])
        jobs = list_job_records_for_user(
            user_id=user_id,
            limit=limit,
            include_terminal=include_terminal,
        )
        return [
            self._maybe_mark_missing_job_as_orphaned(job, user_id=user_id)
            for job in jobs
        ]


def _job_sort_key(value) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_terminal_status(status: Any) -> bool:
    return str(status or "") in SCRAPE_JOB_TERMINAL_STATUSES


def _merge_job_payload(
    stored_status: Optional[dict[str, Any]],
    live_status: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if stored_status is None and live_status is None:
        return None
    if stored_status is None:
        return normalize_scrape_job_payload(live_status)
    if live_status is None:
        return normalize_scrape_job_payload(stored_status)

    merged = normalize_scrape_job_payload(stored_status)
    live_normalized = normalize_scrape_job_payload(live_status)

    if not (_is_terminal_status(merged.get("status")) and not _is_terminal_status(live_normalized.get("status"))):
        for field in ("status", "elapsed_seconds", "queue_position", "finished_at"):
            if live_normalized.get(field) is not None:
                merged[field] = live_normalized[field]
    else:
        for field in ("elapsed_seconds", "queue_position"):
            if live_normalized.get(field) is not None:
                merged[field] = live_normalized[field]

    for field in ("started_at", "updated_at"):
        if live_normalized.get(field) is not None:
            merged[field] = live_normalized[field]

    if live_normalized.get("result") is not None or merged.get("result") is None:
        merged["result"] = live_normalized.get("result")

    if live_normalized.get("error") and not _is_terminal_status(merged.get("status")):
        merged["error"] = live_normalized["error"]

    if live_normalized.get("created_at") is not None:
        merged["created_at"] = live_normalized["created_at"]

    merged_context = dict(merged.get("context") or {})
    merged_context.update(live_normalized.get("context") or {})
    merged["context"] = merged_context
    merged["mode"] = derive_scrape_job_mode(merged)
    merged["error_payload"] = build_scrape_job_error_payload(merged)
    merged["result_summary"] = build_scrape_job_result_summary(merged)
    return merged


def _get_configured_queue_backend_name() -> str:
    if has_app_context():
        configured = current_app.config.get("SCRAPE_QUEUE_BACKEND")
        if configured:
            return str(configured).strip().lower()

    return str(os.environ.get("SCRAPE_QUEUE_BACKEND", "inmemory") or "inmemory").strip().lower()


def _get_configured_redis_url() -> str:
    if has_app_context():
        configured = current_app.config.get("REDIS_URL")
        if configured:
            return str(configured).strip()
    return str(os.environ.get("REDIS_URL", "redis://localhost:6379/0")).strip()


def _get_configured_queue_name() -> str:
    if has_app_context():
        configured = current_app.config.get("SCRAPE_QUEUE_NAME")
        if configured:
            return str(configured).strip()
    return str(os.environ.get("SCRAPE_QUEUE_NAME", "scrape")).strip()


@lru_cache(maxsize=16)
def _build_queue_backend(cache_key: tuple[str, str, str]) -> QueueBackend:
    backend_name, redis_url, queue_name = cache_key
    if backend_name == "inmemory":
        return InMemoryQueueBackend()
    if backend_name == "rq":
        return RQQueueBackend(redis_url=redis_url, queue_name=queue_name)
    raise RuntimeError(f"Unsupported SCRAPE_QUEUE_BACKEND: {backend_name}")


def get_queue_backend() -> QueueBackend:
    return _build_queue_backend(
        (
            _get_configured_queue_backend_name(),
            _get_configured_redis_url(),
            _get_configured_queue_name(),
        )
    )
