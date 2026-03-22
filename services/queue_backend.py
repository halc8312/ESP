"""
Queue backend selection and scrape job contract helpers.
"""
from __future__ import annotations

import os
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
        "finished_at": job_payload.get("finished_at"),
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

    def enqueue(
        self,
        site: str,
        task_fn,
        task_args: tuple = (),
        task_kwargs: dict | None = None,
        user_id: int | None = None,
        context: Optional[dict] = None,
    ) -> str:
        return self._get_queue().enqueue(
            site=site,
            task_fn=task_fn,
            task_args=task_args,
            task_kwargs=task_kwargs,
            user_id=user_id,
            context=context,
        )

    def get_status(self, job_id: str, user_id: int | None = None) -> Optional[dict[str, Any]]:
        return self._get_queue().get_status(job_id, user_id=user_id)

    def get_jobs_for_user(
        self,
        user_id: int,
        limit: int = 5,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        return self._get_queue().get_jobs_for_user(
            user_id=user_id,
            limit=limit,
            include_terminal=include_terminal,
        )


def _get_configured_queue_backend_name() -> str:
    if has_app_context():
        configured = current_app.config.get("SCRAPE_QUEUE_BACKEND")
        if configured:
            return str(configured).strip().lower()

    return str(os.environ.get("SCRAPE_QUEUE_BACKEND", "inmemory") or "inmemory").strip().lower()


@lru_cache(maxsize=8)
def _build_queue_backend(backend_name: str) -> QueueBackend:
    if backend_name == "inmemory":
        return InMemoryQueueBackend()
    raise RuntimeError(f"Unsupported SCRAPE_QUEUE_BACKEND: {backend_name}")


def get_queue_backend() -> QueueBackend:
    return _build_queue_backend(_get_configured_queue_backend_name())
