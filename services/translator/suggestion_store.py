"""
Persistence helpers for :class:`models.TranslationSuggestion`.

Kept intentionally thin — routes and job handlers go through these
functions instead of writing raw SQLAlchemy so that the status machine
(``queued -> running -> succeeded | failed | applied | rejected``) lives
in one place.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from database import SessionLocal
from models import TranslationSuggestion
from time_utils import utc_now


logger = logging.getLogger("services.translator.suggestion_store")


VALID_SCOPES = frozenset({"title", "description", "full"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "applied", "rejected"})


def _session_from_kwarg(session: Optional[Session]) -> tuple[Session, bool]:
    if session is not None:
        return session, False
    return SessionLocal(), True


def create_suggestion(
    *,
    job_id: str,
    product_id: int,
    user_id: int,
    scope: str,
    provider: str,
    source_title: Optional[str],
    source_description: Optional[str],
    source_title_hash: Optional[str],
    source_description_hash: Optional[str],
    session: Optional[Session] = None,
) -> TranslationSuggestion:
    """Insert a new suggestion row in the ``queued`` state."""
    if scope not in VALID_SCOPES:
        raise ValueError(f"Unsupported suggestion scope: {scope!r}")

    session_db, owns_session = _session_from_kwarg(session)
    try:
        now = utc_now()
        row = TranslationSuggestion(
            job_id=job_id,
            product_id=product_id,
            user_id=user_id,
            scope=scope,
            provider=provider,
            source_title=source_title,
            source_description=source_description,
            source_title_hash=source_title_hash,
            source_description_hash=source_description_hash,
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
            session_db.query(TranslationSuggestion)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        if row is None:
            logger.warning("translation suggestion not found while marking running: %s", job_id)
            return
        if row.status in TERMINAL_STATUSES:
            # Already done; don't regress.
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
    translated_title: Optional[str],
    translated_description: Optional[str],
    session: Optional[Session] = None,
) -> None:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        row = (
            session_db.query(TranslationSuggestion)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        if row is None:
            logger.warning("translation suggestion not found while marking success: %s", job_id)
            return
        now = utc_now()
        row.status = "succeeded"
        row.translated_title = translated_title
        row.translated_description = translated_description
        row.error_message = None
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


def mark_failed(
    job_id: str,
    *,
    error_message: str,
    session: Optional[Session] = None,
) -> None:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        row = (
            session_db.query(TranslationSuggestion)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        if row is None:
            logger.warning("translation suggestion not found while marking failed: %s", job_id)
            return
        now = utc_now()
        row.status = "failed"
        row.error_message = error_message[:2000] if error_message else ""
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
) -> Optional[TranslationSuggestion]:
    """Transition an existing suggestion to ``applied`` or ``rejected``."""
    if status not in {"applied", "rejected"}:
        raise ValueError(f"Unsupported terminal status: {status!r}")

    session_db, owns_session = _session_from_kwarg(session)
    try:
        row = (
            session_db.query(TranslationSuggestion)
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


def get_suggestion_by_job_id(
    job_id: str,
    *,
    user_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> Optional[TranslationSuggestion]:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        query = session_db.query(TranslationSuggestion).filter_by(job_id=job_id)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return query.one_or_none()
    finally:
        if owns_session:
            session_db.close()


def list_suggestions_for_product(
    product_id: int,
    *,
    user_id: Optional[int] = None,
    limit: int = 5,
    session: Optional[Session] = None,
) -> list[TranslationSuggestion]:
    session_db, owns_session = _session_from_kwarg(session)
    try:
        query = (
            session_db.query(TranslationSuggestion)
            .filter_by(product_id=product_id)
            .order_by(TranslationSuggestion.created_at.desc())
        )
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return list(query.limit(max(1, min(limit, 20))).all())
    finally:
        if owns_session:
            session_db.close()


def serialize_suggestion(row: TranslationSuggestion) -> dict[str, Any]:
    """Convert a row to a JSON-safe payload for the polling API."""
    completed_at = row.completed_at.isoformat() if row.completed_at else None
    return {
        "job_id": row.job_id,
        "product_id": row.product_id,
        "scope": row.scope,
        "provider": row.provider,
        "status": row.status,
        "translated_title": row.translated_title,
        "translated_description": row.translated_description,
        "source_title": row.source_title,
        "source_description": row.source_description,
        "source_title_hash": row.source_title_hash,
        "source_description_hash": row.source_description_hash,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "completed_at": completed_at,
    }


def serialize_suggestions(rows: Iterable[TranslationSuggestion]) -> list[dict[str, Any]]:
    return [serialize_suggestion(row) for row in rows]
