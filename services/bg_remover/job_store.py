"""
Persistence helpers for :class:`models.ImageProcessingJob`.

Routes and the worker task go through these helpers instead of writing
raw SQLAlchemy so the status machine
(``queued -> running -> succeeded | failed | applied | rejected``)
lives in one place and is easier to audit for the user/shop isolation
invariant.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from database import SessionLocal
from models import ImageProcessingJob
from time_utils import utc_now


logger = logging.getLogger("services.bg_remover.job_store")


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "applied", "rejected"})
VALID_OPERATIONS = frozenset({"bg_remove"})


def _session_from_kwarg(session: Optional[Session]) -> tuple[Session, bool]:
    if session is not None:
        return session, False
    return SessionLocal(), True


def create_job(
    *,
    job_id: str,
    product_id: int,
    user_id: int,
    source_image_url: str,
    provider: str,
    operation: str = "bg_remove",
    session: Optional[Session] = None,
) -> ImageProcessingJob:
    if operation not in VALID_OPERATIONS:
        raise ValueError(f"Unsupported image operation: {operation!r}")

    session_db, owns_session = _session_from_kwarg(session)
    try:
        now = utc_now()
        row = ImageProcessingJob(
            job_id=job_id,
            product_id=product_id,
            user_id=user_id,
            source_image_url=source_image_url,
            provider=provider,
            operation=operation,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        session_db.add(row)
        if owns_session:
            session_db.commit()
        else:
            session_db.flush()
        session_db.refresh(row)
        return row
    except Exception:
        if owns_session:
            session_db.rollback()
        raise
    finally:
        if owns_session:
            session_db.close()


def mark_running(job_id: str, *, session: Optional[Session] = None) -> None:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        row = (
            session_db.query(ImageProcessingJob)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        if row is None:
            logger.warning("bg job not found while marking running: %s", job_id)
            return
        if row.status in TERMINAL_STATUSES:
            return
        row.status = "running"
        row.updated_at = utc_now()
        if owns_session:
            session_db.commit()
    except Exception:
        if owns_session:
            session_db.rollback()
        raise
    finally:
        if owns_session:
            session_db.close()


def mark_succeeded(
    job_id: str,
    *,
    result_image_url: str,
    session: Optional[Session] = None,
) -> Optional[ImageProcessingJob]:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        row = (
            session_db.query(ImageProcessingJob)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        if row is None:
            logger.warning("bg job not found while marking succeeded: %s", job_id)
            return None
        if row.status in TERMINAL_STATUSES:
            # Preserve operator decisions (applied/rejected) and any prior
            # terminal state instead of letting a late worker success
            # overwrite them.
            logger.info(
                "bg job %s already in terminal status %s; not marking succeeded",
                job_id,
                row.status,
            )
            return None
        now = utc_now()
        row.status = "succeeded"
        row.result_image_url = result_image_url
        row.error_message = None
        row.completed_at = now
        row.updated_at = now
        if owns_session:
            session_db.commit()
            session_db.refresh(row)
        return row
    except Exception:
        if owns_session:
            session_db.rollback()
        raise
    finally:
        if owns_session:
            session_db.close()


def mark_failed(
    job_id: str,
    *,
    error_message: str,
    session: Optional[Session] = None,
) -> None:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        row = (
            session_db.query(ImageProcessingJob)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        if row is None:
            logger.warning("bg job not found while marking failed: %s", job_id)
            return
        if row.status in TERMINAL_STATUSES:
            # The worker's late failure path must not clobber an operator's
            # reject/apply decision or an earlier succeeded result.
            logger.info(
                "bg job %s already in terminal status %s; not marking failed",
                job_id,
                row.status,
            )
            return
        now = utc_now()
        row.status = "failed"
        row.error_message = (error_message or "")[:2000]
        row.completed_at = now
        row.updated_at = now
        if owns_session:
            session_db.commit()
    except Exception:
        if owns_session:
            session_db.rollback()
        raise
    finally:
        if owns_session:
            session_db.close()


def mark_terminal_state(
    job_id: str,
    *,
    status: str,
    session: Optional[Session] = None,
) -> Optional[ImageProcessingJob]:
    """Transition an existing job to ``applied`` or ``rejected``."""
    if status not in {"applied", "rejected"}:
        raise ValueError(f"Unsupported terminal status: {status!r}")

    session_db, owns_session = _session_from_kwarg(session)
    try:
        row = (
            session_db.query(ImageProcessingJob)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        if row is None:
            return None
        row.status = status
        row.updated_at = utc_now()
        if owns_session:
            session_db.commit()
            session_db.refresh(row)
        return row
    except Exception:
        if owns_session:
            session_db.rollback()
        raise
    finally:
        if owns_session:
            session_db.close()


def get_job_by_job_id(
    job_id: str,
    *,
    user_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> Optional[ImageProcessingJob]:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        query = session_db.query(ImageProcessingJob).filter_by(job_id=job_id)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return query.one_or_none()
    finally:
        if owns_session:
            session_db.close()


def list_jobs_for_product(
    product_id: int,
    *,
    user_id: Optional[int] = None,
    limit: int = 20,
    session: Optional[Session] = None,
) -> list[ImageProcessingJob]:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        query = (
            session_db.query(ImageProcessingJob)
            .filter_by(product_id=product_id)
            .order_by(ImageProcessingJob.created_at.desc())
        )
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return list(query.limit(max(1, min(limit, 100))).all())
    finally:
        if owns_session:
            session_db.close()


def serialize_job(row: ImageProcessingJob) -> dict[str, Any]:
    """Convert a row to a JSON-safe payload for polling / UI rendering."""
    return {
        "job_id": row.job_id,
        "product_id": row.product_id,
        "operation": row.operation,
        "provider": row.provider,
        "status": row.status,
        "source_image_url": row.source_image_url,
        "result_image_url": row.result_image_url,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def serialize_jobs(rows: Iterable[ImageProcessingJob]) -> list[dict[str, Any]]:
    return [serialize_job(row) for row in rows]
