"""
Durable scrape job state persistence.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

from flask import current_app, has_app_context

from database import SessionLocal
from models import ScrapeJob, ScrapeJobEvent
from time_utils import utc_now


SCRAPE_JOB_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _utcnow() -> datetime:
    return utc_now()


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_loads(value: Optional[str]):
    if not value:
        return None
    return json.loads(value)


def _derive_mode(context: Optional[dict[str, Any]], request_payload: Optional[dict[str, Any]], mode: str | None) -> str:
    if mode:
        return str(mode)
    persisted_context = context or {}
    if persisted_context.get("mode"):
        return str(persisted_context["mode"])
    if isinstance(request_payload, dict) and request_payload.get("persist_to_db") is False:
        return "preview"
    if persisted_context.get("persist_to_db") is False:
        return "preview"
    return "persist"


def _build_result_summary(result: Any) -> Optional[dict[str, int]]:
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


def _record_event(session, job_id: str, event_type: str, payload: Any = None) -> None:
    session.add(
        ScrapeJobEvent(
            job_id=job_id,
            event_type=event_type,
            payload=_json_dumps(payload),
            created_at=_utcnow(),
        )
    )


def create_job_record(
    job_id: str,
    site: str,
    user_id: int | None = None,
    context: Optional[dict[str, Any]] = None,
    request_payload: Optional[dict[str, Any]] = None,
    mode: str | None = None,
) -> str:
    session = SessionLocal()
    now = _utcnow()
    try:
        record = session.query(ScrapeJob).filter_by(job_id=job_id).one_or_none()
        if record is None:
            record = ScrapeJob(job_id=job_id, logical_job_id=job_id)
            session.add(record)

        record.site = site
        record.status = "queued"
        record.mode = _derive_mode(context, request_payload, mode)
        record.requested_by = user_id
        record.request_payload = _json_dumps(request_payload)
        record.context_payload = _json_dumps(context or {})
        record.updated_at = now
        if record.created_at is None:
            record.created_at = now

        _record_event(session, job_id, "queued", {"site": site})
        session.commit()
    finally:
        session.close()

    return job_id


def mark_job_running(job_id: str, event_payload: Any = None) -> None:
    session = SessionLocal()
    now = _utcnow()
    try:
        record = session.query(ScrapeJob).filter_by(job_id=job_id).one_or_none()
        if record is None:
            return
        record.status = "running"
        if record.started_at is None:
            record.started_at = now
        record.updated_at = now
        _record_event(session, job_id, "running", event_payload)
        session.commit()
    finally:
        session.close()


def mark_job_heartbeat(job_id: str) -> None:
    session = SessionLocal()
    now = _utcnow()
    try:
        record = session.query(ScrapeJob).filter_by(job_id=job_id).one_or_none()
        if record is None or record.status != "running":
            return
        record.updated_at = now
        session.commit()
    finally:
        session.close()


def mark_job_completed(job_id: str, result: Any) -> None:
    session = SessionLocal()
    now = _utcnow()
    try:
        record = session.query(ScrapeJob).filter_by(job_id=job_id).one_or_none()
        if record is None:
            return
        if record.started_at is None:
            record.started_at = now
        record.status = "completed"
        record.result_payload = _json_dumps(result)
        record.result_summary = _json_dumps(_build_result_summary(result))
        record.error_message = None
        record.error_payload = None
        record.finished_at = now
        record.updated_at = now
        _record_event(session, job_id, "completed", _build_result_summary(result))
        session.commit()
    finally:
        session.close()


def mark_job_failed(job_id: str, error_message: str, error_payload: Any = None) -> None:
    session = SessionLocal()
    now = _utcnow()
    try:
        record = session.query(ScrapeJob).filter_by(job_id=job_id).one_or_none()
        if record is None:
            return
        if record.started_at is None:
            record.started_at = now
        record.status = "failed"
        record.error_message = str(error_message)
        record.error_payload = _json_dumps(
            error_payload or {"message": str(error_message), "kind": "job_failed"}
        )
        record.finished_at = now
        record.updated_at = now
        _record_event(session, job_id, "failed", {"message": str(error_message)})
        session.commit()
    finally:
        session.close()


def _get_stall_timeout_seconds(stall_timeout_seconds: int | None = None) -> int:
    if stall_timeout_seconds is not None:
        return max(1, int(stall_timeout_seconds))
    if has_app_context():
        configured = current_app.config.get("SCRAPE_JOB_STALL_TIMEOUT_SECONDS")
        if configured:
            return max(1, int(configured))
    return max(1, int(os.environ.get("SCRAPE_JOB_STALL_TIMEOUT_SECONDS", "900")))


def _resolve_stall_reference(record: ScrapeJob):
    return record.updated_at or record.started_at or record.created_at


def _resolve_orphan_reference(record: ScrapeJob):
    return record.updated_at or record.started_at or record.created_at


def _stall_error_message() -> str:
    return "ジョブが停止した可能性があります。再実行してください。"


def _stall_error_payload(timeout_seconds: int) -> dict[str, Any]:
    return {
        "message": _stall_error_message(),
        "kind": "job_stalled",
        "stalled_after_seconds": timeout_seconds,
    }


def _orphan_error_message() -> str:
    return "キュー上のジョブが見つかりません。再実行してください。"


def _orphan_error_payload(timeout_seconds: int) -> dict[str, Any]:
    return {
        "message": _orphan_error_message(),
        "kind": "job_orphaned",
        "orphaned_after_seconds": timeout_seconds,
    }


def maybe_mark_job_stalled(job_id: str, stall_timeout_seconds: int | None = None) -> bool:
    session = SessionLocal()
    now = _utcnow()
    try:
        record = session.query(ScrapeJob).filter_by(job_id=job_id).one_or_none()
        if record is None or record.status != "running":
            return False

        timeout_seconds = _get_stall_timeout_seconds(stall_timeout_seconds)
        reference_time = _resolve_stall_reference(record)
        if reference_time is None:
            return False

        age_seconds = (now - reference_time).total_seconds()
        if age_seconds < timeout_seconds:
            return False

        record.status = "failed"
        record.error_message = _stall_error_message()
        record.error_payload = _json_dumps(_stall_error_payload(timeout_seconds))
        record.finished_at = now
        record.updated_at = now
        _record_event(
            session,
            job_id,
            "stalled",
            {"stalled_after_seconds": timeout_seconds},
        )
        session.commit()
        return True
    finally:
        session.close()


def _get_orphan_timeout_seconds(orphan_timeout_seconds: int | None = None) -> int:
    if orphan_timeout_seconds is not None:
        return max(1, int(orphan_timeout_seconds))
    if has_app_context():
        configured = current_app.config.get("SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS")
        if configured:
            return max(1, int(configured))
    return max(1, int(os.environ.get("SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS", "60")))


def maybe_mark_job_orphaned(job_id: str, orphan_timeout_seconds: int | None = None) -> bool:
    session = SessionLocal()
    now = _utcnow()
    try:
        record = session.query(ScrapeJob).filter_by(job_id=job_id).one_or_none()
        if record is None or record.status in SCRAPE_JOB_TERMINAL_STATUSES:
            return False

        timeout_seconds = _get_orphan_timeout_seconds(orphan_timeout_seconds)
        reference_time = _resolve_orphan_reference(record)
        if reference_time is None:
            return False

        age_seconds = (now - reference_time).total_seconds()
        if age_seconds < timeout_seconds:
            return False

        record.status = "failed"
        record.error_message = _orphan_error_message()
        record.error_payload = _json_dumps(_orphan_error_payload(timeout_seconds))
        record.finished_at = now
        record.updated_at = now
        _record_event(
            session,
            job_id,
            "orphaned",
            {"orphaned_after_seconds": timeout_seconds},
        )
        session.commit()
        return True
    finally:
        session.close()


def reconcile_stalled_jobs(stall_timeout_seconds: int | None = None) -> list[str]:
    session = SessionLocal()
    now = _utcnow()
    timeout_seconds = _get_stall_timeout_seconds(stall_timeout_seconds)
    reconciled_job_ids: list[str] = []
    try:
        running_jobs = session.query(ScrapeJob).filter_by(status="running").all()
        for record in running_jobs:
            reference_time = _resolve_stall_reference(record)
            if reference_time is None:
                continue

            age_seconds = (now - reference_time).total_seconds()
            if age_seconds < timeout_seconds:
                continue

            record.status = "failed"
            record.error_message = _stall_error_message()
            record.error_payload = _json_dumps(_stall_error_payload(timeout_seconds))
            record.finished_at = now
            record.updated_at = now
            _record_event(
                session,
                record.job_id,
                "stalled",
                {"stalled_after_seconds": timeout_seconds},
            )
            reconciled_job_ids.append(record.job_id)

        if reconciled_job_ids:
            session.commit()
        return reconciled_job_ids
    finally:
        session.close()


def _serialize_job_record(record: ScrapeJob) -> dict[str, Any]:
    finished_at = record.finished_at
    created_at = record.created_at
    elapsed_seconds = None
    if created_at is not None:
        elapsed_reference = finished_at or _utcnow()
        elapsed_seconds = round((elapsed_reference - created_at).total_seconds(), 1)

    return {
        "job_id": record.job_id,
        "site": record.site,
        "status": record.status,
        "result": _json_loads(record.result_payload),
        "error": record.error_message,
        "elapsed_seconds": elapsed_seconds,
        "queue_position": None,
        "context": _json_loads(record.context_payload) or {},
        "created_at": created_at,
        "started_at": record.started_at,
        "finished_at": finished_at,
        "updated_at": record.updated_at,
        "result_summary": _json_loads(record.result_summary),
        "error_payload": _json_loads(record.error_payload),
        "mode": record.mode,
        "request_payload": _json_loads(record.request_payload),
    }


def get_job_record(job_id: str, user_id: int | None = None) -> Optional[dict[str, Any]]:
    session = SessionLocal()
    try:
        query = session.query(ScrapeJob).filter_by(job_id=job_id)
        if user_id is not None:
            query = query.filter_by(requested_by=user_id)
        record = query.one_or_none()
        if record is None:
            return None
        return _serialize_job_record(record)
    finally:
        session.close()


def list_job_records_for_user(
    user_id: int,
    limit: int = 5,
    include_terminal: bool = True,
) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit or 5))
    session = SessionLocal()
    try:
        query = session.query(ScrapeJob).filter_by(requested_by=user_id)
        if not include_terminal:
            query = query.filter(~ScrapeJob.status.in_(tuple(SCRAPE_JOB_TERMINAL_STATUSES)))
        records = query.order_by(ScrapeJob.created_at.desc()).limit(safe_limit).all()
        return [_serialize_job_record(record) for record in records]
    finally:
        session.close()


def get_job_backlog_snapshot() -> dict[str, Any]:
    session = SessionLocal()
    now = _utcnow()
    try:
        queued_jobs = session.query(ScrapeJob).filter_by(status="queued").order_by(ScrapeJob.created_at.asc()).all()
        running_jobs = session.query(ScrapeJob).filter_by(status="running").order_by(ScrapeJob.updated_at.asc()).all()

        oldest_queued = queued_jobs[0] if queued_jobs else None
        oldest_running = running_jobs[0] if running_jobs else None
        oldest_running_reference = _resolve_stall_reference(oldest_running) if oldest_running is not None else None

        return {
            "captured_at": now,
            "queued_count": len(queued_jobs),
            "running_count": len(running_jobs),
            "oldest_queued_job_id": oldest_queued.job_id if oldest_queued is not None else None,
            "oldest_running_job_id": oldest_running.job_id if oldest_running is not None else None,
            "oldest_queued_age_seconds": round((now - oldest_queued.created_at).total_seconds(), 1)
            if oldest_queued is not None and oldest_queued.created_at is not None
            else None,
            "oldest_running_age_seconds": round((now - oldest_running_reference).total_seconds(), 1)
            if oldest_running_reference is not None
            else None,
        }
    finally:
        session.close()
