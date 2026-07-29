"""
Persistence helpers for :class:`models.TranslationSuggestion`.

Kept intentionally thin — routes and job handlers go through these
functions instead of writing raw SQLAlchemy so that the status machine
(``queued -> running -> succeeded | failed | applied | rejected``) lives
in one place.
"""
from __future__ import annotations

import os
import uuid
from datetime import timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Product, TranslationSuggestion
from time_utils import utc_now


VALID_SCOPES = frozenset({"title", "description", "full"})
DEFAULT_TRANSLATION_LEASE_SECONDS = 1800


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
    auto_apply: bool = False,
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
            auto_apply=auto_apply,
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


def _resolve_lease_seconds(lease_seconds: Optional[int] = None) -> int:
    raw_value = (
        lease_seconds
        if lease_seconds is not None
        else os.environ.get(
            "TRANSLATION_JOB_LEASE_SECONDS",
            os.environ.get(
                "MEDIA_JOB_TIMEOUT_SECONDS",
                str(DEFAULT_TRANSLATION_LEASE_SECONDS),
            ),
        )
    )
    try:
        return max(60, int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_TRANSLATION_LEASE_SECONDS


def _recoverable_claim_filter(now, stale_cutoff):
    return or_(
        TranslationSuggestion.status == "queued",
        and_(
            TranslationSuggestion.status == "running",
            or_(
                TranslationSuggestion.lease_expires_at <= now,
                and_(
                    TranslationSuggestion.lease_expires_at.is_(None),
                    TranslationSuggestion.updated_at <= stale_cutoff,
                ),
            ),
        ),
    )


def mark_running(
    job_id: str,
    *,
    worker_token: str,
    lease_seconds: Optional[int] = None,
    session: Optional[Session] = None,
) -> bool:
    """Atomically claim a queued or expired suggestion with a fencing token."""
    normalized_worker_token = str(worker_token or "").strip()
    if not normalized_worker_token:
        raise ValueError("worker_token is required")

    session_db, owns_session = _session_from_kwarg(session)
    try:
        now = utc_now()
        resolved_lease_seconds = _resolve_lease_seconds(lease_seconds)
        stale_cutoff = now - timedelta(seconds=resolved_lease_seconds)
        updated_count = (
            session_db.query(TranslationSuggestion)
            .filter(
                TranslationSuggestion.job_id == job_id,
                _recoverable_claim_filter(now, stale_cutoff),
            )
            .update(
                {
                    TranslationSuggestion.status: "running",
                    TranslationSuggestion.worker_token: normalized_worker_token,
                    TranslationSuggestion.lease_expires_at: (
                        now + timedelta(seconds=resolved_lease_seconds)
                    ),
                    TranslationSuggestion.error_message: None,
                    TranslationSuggestion.completed_at: None,
                    TranslationSuggestion.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if owns_session:
            session_db.commit()
        return updated_count == 1
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
    worker_token: str,
    translated_title: Optional[str],
    translated_description: Optional[str],
    session: Optional[Session] = None,
) -> bool:
    """Store a worker result only while the suggestion is worker-owned.

    The status predicate is part of the UPDATE.  A reject/apply committed
    between a worker's read and write therefore wins instead of being
    overwritten by a stale ORM object.
    """
    session_db, owns_session = _session_from_kwarg(session)
    try:
        normalized_worker_token = str(worker_token or "").strip()
        if not normalized_worker_token:
            raise ValueError("worker_token is required")
        now = utc_now()
        updated_count = (
            session_db.query(TranslationSuggestion)
            .filter(
                TranslationSuggestion.job_id == job_id,
                TranslationSuggestion.status == "running",
                TranslationSuggestion.worker_token == normalized_worker_token,
            )
            .update(
                {
                    TranslationSuggestion.status: "succeeded",
                    TranslationSuggestion.translated_title: translated_title,
                    TranslationSuggestion.translated_description: translated_description,
                    TranslationSuggestion.error_message: None,
                    TranslationSuggestion.worker_token: None,
                    TranslationSuggestion.lease_expires_at: None,
                    TranslationSuggestion.completed_at: now,
                    TranslationSuggestion.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if owns_session:
            session_db.commit()
        return updated_count == 1
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
    worker_token: str,
    error_message: str,
    session: Optional[Session] = None,
) -> bool:
    """Store a worker failure without regressing an operator decision."""
    session_db, owns_session = _session_from_kwarg(session)
    try:
        normalized_worker_token = str(worker_token or "").strip()
        if not normalized_worker_token:
            raise ValueError("worker_token is required")
        now = utc_now()
        updated_count = (
            session_db.query(TranslationSuggestion)
            .filter(
                TranslationSuggestion.job_id == job_id,
                TranslationSuggestion.status == "running",
                TranslationSuggestion.worker_token == normalized_worker_token,
            )
            .update(
                {
                    TranslationSuggestion.status: "failed",
                    TranslationSuggestion.error_message: (
                        error_message[:2000] if error_message else ""
                    ),
                    TranslationSuggestion.worker_token: None,
                    TranslationSuggestion.lease_expires_at: None,
                    TranslationSuggestion.completed_at: now,
                    TranslationSuggestion.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if owns_session:
            session_db.commit()
        return updated_count == 1
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
    user_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> Optional[TranslationSuggestion]:
    """CAS a suggestion into an operator-owned terminal state."""
    if status not in {"applied", "rejected"}:
        raise ValueError(f"Unsupported terminal status: {status!r}")

    session_db, owns_session = _session_from_kwarg(session)
    try:
        eligible_statuses = (
            ("succeeded",)
            if status == "applied"
            else ("queued", "running", "succeeded", "failed")
        )
        query = (
            session_db.query(TranslationSuggestion)
            .filter(
                TranslationSuggestion.job_id == job_id,
                TranslationSuggestion.status.in_(eligible_statuses),
            )
        )
        if user_id is not None:
            query = query.filter(TranslationSuggestion.user_id == user_id)
        query.update(
            {
                TranslationSuggestion.status: status,
                TranslationSuggestion.worker_token: None,
                TranslationSuggestion.lease_expires_at: None,
                TranslationSuggestion.updated_at: utc_now(),
            },
            synchronize_session=False,
        )
        if owns_session:
            session_db.commit()
        else:
            session_db.expire_all()

        result_query = session_db.query(TranslationSuggestion).filter_by(job_id=job_id)
        if user_id is not None:
            result_query = result_query.filter_by(user_id=user_id)
        return result_query.one_or_none()
    except Exception:
        if owns_session:
            session_db.rollback()
        raise
    finally:
        if owns_session:
            session_db.close()


def _record_recovery_enqueue_error(job_id: str, error_message: str) -> None:
    """Keep an ambiguously enqueued recovery job retryable.

    An RQ enqueue call can fail after Redis accepted the job, and a stale
    ``queued`` suggestion may still have its original RQ entry.  Moving the
    suggestion to ``failed`` here would make either live worker skip it.  Keep
    the durable state queued, record the operational error, and advance
    ``updated_at`` to provide one lease interval of retry backoff.
    """
    session_db = SessionLocal()
    try:
        now = utc_now()
        (
            session_db.query(TranslationSuggestion)
            .filter(
                TranslationSuggestion.job_id == job_id,
                TranslationSuggestion.status == "queued",
            )
            .update(
                {
                    TranslationSuggestion.status: "queued",
                    TranslationSuggestion.error_message: error_message[:2000],
                    TranslationSuggestion.worker_token: None,
                    TranslationSuggestion.lease_expires_at: None,
                    TranslationSuggestion.completed_at: None,
                    TranslationSuggestion.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        session_db.commit()
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


def recover_expired_translation_suggestions(
    *,
    limit: int = 25,
    lease_seconds: Optional[int] = None,
    now=None,
) -> dict[str, Any]:
    """Requeue expired running jobs and stale queued jobs.

    Resetting and enqueueing are intentionally fencing-safe.  If the original
    RQ entry also wakes up, only one worker can claim the queued row.
    """
    resolved_lease_seconds = _resolve_lease_seconds(lease_seconds)
    recovery_now = now or utc_now()
    stale_cutoff = recovery_now - timedelta(seconds=resolved_lease_seconds)
    safe_limit = max(1, min(int(limit or 25), 100))
    recovered_job_ids: list[str] = []

    session_db = SessionLocal()
    try:
        candidates = (
            session_db.query(TranslationSuggestion.job_id)
            .filter(
                or_(
                    and_(
                        TranslationSuggestion.status == "running",
                        or_(
                            TranslationSuggestion.lease_expires_at <= recovery_now,
                            and_(
                                TranslationSuggestion.lease_expires_at.is_(None),
                                TranslationSuggestion.updated_at <= stale_cutoff,
                            ),
                        ),
                    ),
                    and_(
                        TranslationSuggestion.status == "queued",
                        TranslationSuggestion.updated_at <= stale_cutoff,
                    ),
                )
            )
            .order_by(TranslationSuggestion.updated_at.asc())
            .limit(safe_limit)
            .all()
        )
        for (job_id,) in candidates:
            updated_count = (
                session_db.query(TranslationSuggestion)
                .filter(
                    TranslationSuggestion.job_id == job_id,
                    or_(
                        and_(
                            TranslationSuggestion.status == "running",
                            or_(
                                TranslationSuggestion.lease_expires_at
                                <= recovery_now,
                                and_(
                                    TranslationSuggestion.lease_expires_at.is_(
                                        None
                                    ),
                                    TranslationSuggestion.updated_at
                                    <= stale_cutoff,
                                ),
                            ),
                        ),
                        and_(
                            TranslationSuggestion.status == "queued",
                            TranslationSuggestion.updated_at <= stale_cutoff,
                        ),
                    ),
                )
                .update(
                    {
                        TranslationSuggestion.status: "queued",
                        TranslationSuggestion.worker_token: None,
                        TranslationSuggestion.lease_expires_at: None,
                        TranslationSuggestion.error_message: None,
                        TranslationSuggestion.completed_at: None,
                        TranslationSuggestion.updated_at: recovery_now,
                    },
                    synchronize_session=False,
                )
            )
            if updated_count == 1:
                recovered_job_ids.append(str(job_id))
        session_db.commit()
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()

    enqueued: list[str] = []
    failed: list[str] = []
    if recovered_job_ids:
        from services.media_queue import (
            enqueue_media_job,
            resolve_queue_backend_name,
        )

        backend_name = resolve_queue_backend_name()
        for suggestion_job_id in recovered_job_ids:
            try:
                if backend_name == "rq":
                    enqueue_media_job(
                        job_id=f"translation-recovery-{uuid.uuid4().hex}",
                        func="jobs.translation_tasks.execute_translation_job",
                        args=(suggestion_job_id,),
                        description=f"recover translation {suggestion_job_id}",
                    )
                else:
                    from jobs.translation_tasks import execute_translation_job

                    execute_translation_job(suggestion_job_id)
                enqueued.append(suggestion_job_id)
            except Exception as exc:
                failed.append(suggestion_job_id)
                _record_recovery_enqueue_error(
                    suggestion_job_id,
                    f"翻訳処理の再開に失敗しました: {type(exc).__name__}",
                )

    return {
        "recovered": recovered_job_ids,
        "enqueued": enqueued,
        "failed": failed,
    }


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


def apply_suggestion_to_product(
    suggestion: TranslationSuggestion,
    session: Session,
    *,
    preserve_existing: bool = False,
    apply_title: bool = True,
    apply_description: bool = True,
) -> dict[str, str | None]:
    """Apply a succeeded suggestion's translations to the Product.

    Returns a dict of field names that were actually updated on the
    product.  The caller is responsible for committing the session.
    """
    from services.rich_text import normalize_rich_text

    # Claim the suggestion before touching Product.  A concurrent rejection
    # either commits first and makes this UPDATE a no-op, or waits and observes
    # ``applied``.  Product changes and the claim share one transaction.
    claimed_count = (
        session.query(TranslationSuggestion)
        .filter(
            TranslationSuggestion.id == suggestion.id,
            TranslationSuggestion.status == "succeeded",
        )
        .update(
            {
                TranslationSuggestion.status: "applied",
                TranslationSuggestion.updated_at: utc_now(),
            },
            synchronize_session=False,
        )
    )
    if claimed_count != 1:
        session.expire(suggestion)
        return {}

    product = (
        session.query(Product)
        .filter_by(id=suggestion.product_id, user_id=suggestion.user_id)
        .one_or_none()
    )
    if product is None:
        (
            session.query(TranslationSuggestion)
            .filter(
                TranslationSuggestion.id == suggestion.id,
                TranslationSuggestion.status == "applied",
            )
            .update(
                {TranslationSuggestion.status: "succeeded"},
                synchronize_session=False,
            )
        )
        session.expire(suggestion)
        return {}

    changes: dict[str, str | None] = {}

    def target_can_be_replaced(
        *,
        current_value: Optional[str],
        current_source_hash: Optional[str],
        manually_edited: bool,
        translated_field,
        source_hash_field,
    ) -> bool:
        if not preserve_existing:
            return True
        if manually_edited:
            # This includes an intentional manual clear, which cannot be
            # inferred from the nullable target value alone.
            return False
        if not (current_value or "").strip():
            return True
        if not current_source_hash:
            # A populated target without a translation source hash is a
            # manual value (including a manual edit made while this job was
            # queued), so automatic application must leave it alone.
            return False

        # Older product-edit code did not clear the source hash after a manual
        # edit.  Treat the value as auto-managed only when it still exactly
        # matches a previously applied suggestion for that source.
        return (
            session.query(TranslationSuggestion.id)
            .filter(
                TranslationSuggestion.product_id == product.id,
                TranslationSuggestion.user_id == product.user_id,
                TranslationSuggestion.status == "applied",
                source_hash_field == current_source_hash,
                translated_field == current_value,
                TranslationSuggestion.id != suggestion.id,
            )
            .first()
            is not None
        )

    def matches_expected(column, expected_value):
        return column.is_(None) if expected_value is None else column == expected_value

    def cas_product_field(
        *,
        value_column,
        source_hash_column,
        manual_column,
        expected_value: Optional[str],
        expected_source_hash: Optional[str],
        expected_manually_edited: bool,
        new_value: Optional[str],
        new_source_hash: Optional[str],
    ) -> bool:
        """Update one target only if no editor changed it after our read."""
        updated_count = (
            session.query(Product)
            .filter(
                Product.id == product.id,
                Product.user_id == product.user_id,
                matches_expected(value_column, expected_value),
                matches_expected(source_hash_column, expected_source_hash),
                manual_column.is_(expected_manually_edited),
            )
            .update(
                {
                    value_column: new_value,
                    source_hash_column: new_source_hash,
                    manual_column: False,
                    Product.updated_at: utc_now(),
                },
                synchronize_session=False,
            )
        )
        return updated_count == 1

    if (
        apply_title
        and suggestion.translated_title
        and target_can_be_replaced(
            current_value=product.custom_title_en,
            current_source_hash=product.custom_title_en_source_hash,
            manually_edited=bool(product.custom_title_en_manually_edited),
            translated_field=TranslationSuggestion.translated_title,
            source_hash_field=TranslationSuggestion.source_title_hash,
        )
        and cas_product_field(
            value_column=Product.custom_title_en,
            source_hash_column=Product.custom_title_en_source_hash,
            manual_column=Product.custom_title_en_manually_edited,
            expected_value=product.custom_title_en,
            expected_source_hash=product.custom_title_en_source_hash,
            expected_manually_edited=bool(product.custom_title_en_manually_edited),
            new_value=suggestion.translated_title,
            new_source_hash=suggestion.source_title_hash,
        )
    ):
        changes["custom_title_en"] = suggestion.translated_title

    if apply_description and suggestion.translated_description:
        sanitised = normalize_rich_text(suggestion.translated_description) or None
        if (
            sanitised
            and target_can_be_replaced(
                current_value=product.custom_description_en,
                current_source_hash=product.custom_description_en_source_hash,
                manually_edited=bool(product.custom_description_en_manually_edited),
                translated_field=TranslationSuggestion.translated_description,
                source_hash_field=TranslationSuggestion.source_description_hash,
            )
            and cas_product_field(
                value_column=Product.custom_description_en,
                source_hash_column=Product.custom_description_en_source_hash,
                manual_column=Product.custom_description_en_manually_edited,
                expected_value=product.custom_description_en,
                expected_source_hash=product.custom_description_en_source_hash,
                expected_manually_edited=bool(
                    product.custom_description_en_manually_edited
                ),
                new_value=sanitised,
                new_source_hash=suggestion.source_description_hash,
            )
        ):
            changes["custom_description_en"] = sanitised

    if not changes:
        # No field passed the CAS/ownership checks.  Release the claim in the
        # same transaction so the suggestion remains reviewable.
        (
            session.query(TranslationSuggestion)
            .filter(
                TranslationSuggestion.id == suggestion.id,
                TranslationSuggestion.status == "applied",
            )
            .update(
                {
                    TranslationSuggestion.status: "succeeded",
                    TranslationSuggestion.updated_at: utc_now(),
                },
                synchronize_session=False,
            )
        )

    session.expire(product)
    session.expire(suggestion)
    return changes


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
