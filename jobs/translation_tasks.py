"""
Worker-callable translation task execution.

A translation job:

1. Loads the ``TranslationSuggestion`` row identified by ``job_id`` and
   flips its status to ``running``.
2. Uses the configured :class:`TranslatorBackend` to translate the
   captured source title / description (plain text for the title, HTML
   segmentation for the description so rich-text structure survives).
3. Writes the result back to the suggestion row as ``succeeded`` (or
   ``failed`` with an error message).
4. For registration jobs marked ``auto_apply``, applies the result to the
   product unless the operator has manually edited that English field while
   translation was running. Other jobs remain available for explicit review
   and application from the UI.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from services.rich_text import normalize_rich_text
from services.translator import get_fallback_translator_backend, get_translator_backend
from services.translator.base import TranslationError, TranslatorUnavailableError
from services.translator.suggestion_store import (
    apply_suggestion_to_product,
    get_suggestion_by_job_id,
    mark_failed,
    mark_running,
    mark_succeeded,
)


logger = logging.getLogger("jobs.translation_tasks")


def _translate_with(
    backend: Any,
    scope: str,
    source_title: str,
    source_description: str,
) -> tuple[str | None, str | None]:
    """Translate the parts this job asked for, using the given backend."""
    translated_title: str | None = None
    translated_description: str | None = None

    if scope in {"title", "full"} and source_title.strip():
        translated_title = backend.translate_plain(source_title).strip() or None

    if scope in {"description", "full"} and source_description.strip():
        # Sanitise the source first so the translator never operates on
        # attacker-controlled HTML (the source can come from a scraped
        # marketplace snapshot). normalize_rich_text enforces the same
        # allowlist as the editor via nh3.
        safe_source = normalize_rich_text(source_description)
        if safe_source:
            translated_raw = backend.translate_html(safe_source).strip()
            # Belt-and-braces: sanitise the output too before it gets
            # stored and rendered in the review UI.
            translated_description = normalize_rich_text(translated_raw) or None

    return translated_title, translated_description


def _describe_failure(exc: Exception) -> str:
    """
    Wording for the operator, who sees this next to the translate button.

    The raw text is an English stack of API codes and billing URLs. It tells
    a student nothing they can act on, so the two conditions that actually
    happen get a plain sentence; anything else keeps its original text, which
    is more useful than a vague apology when something unexpected breaks.
    """
    if isinstance(exc, TranslatorUnavailableError):
        return "翻訳サービスに接続できませんでした。管理者にご連絡ください。"
    return str(exc)


def _auto_apply_suggestion(job_id: str) -> None:
    """Apply a succeeded suggestion to the product when auto_apply is set."""
    from database import create_isolated_session

    session_db = create_isolated_session()
    try:
        refreshed = get_suggestion_by_job_id(job_id, session=session_db)
        if refreshed is None or refreshed.status != "succeeded":
            return
        changes = apply_suggestion_to_product(
            refreshed,
            session_db,
            preserve_existing=True,
        )
        if changes:
            session_db.commit()
            logger.info(
                "auto-applied translation %s to product %s: %s",
                job_id,
                refreshed.product_id,
                sorted(changes.keys()),
            )
    except Exception:
        session_db.rollback()
        logger.exception("auto-apply failed for translation job %s", job_id)
    finally:
        session_db.close()


def _persist_translation_result(
    *,
    job_id: str,
    worker_token: str,
    translated_title: str | None,
    translated_description: str | None,
    auto_apply: bool,
) -> bool:
    """Persist a worker result, atomically with automatic Product application."""
    if not auto_apply:
        return mark_succeeded(
            job_id,
            worker_token=worker_token,
            translated_title=translated_title,
            translated_description=translated_description,
        )

    from database import create_isolated_session

    session_db = create_isolated_session()
    try:
        stored = mark_succeeded(
            job_id,
            worker_token=worker_token,
            translated_title=translated_title,
            translated_description=translated_description,
            session=session_db,
        )
        if not stored:
            session_db.rollback()
            return False

        refreshed = get_suggestion_by_job_id(job_id, session=session_db)
        if refreshed is None:
            raise RuntimeError(
                f"translation suggestion disappeared before auto-apply: {job_id}"
            )
        product_id = refreshed.product_id
        changes = apply_suggestion_to_product(
            refreshed,
            session_db,
            preserve_existing=True,
        )
        # With no eligible Product field, apply_suggestion_to_product restores
        # the suggestion to `succeeded`.  Commit that reviewable state together
        # with the translated text; otherwise commit `applied` plus Product.
        session_db.commit()
        if changes:
            logger.info(
                "auto-applied translation %s to product %s: %s",
                job_id,
                product_id,
                sorted(changes.keys()),
            )
        return True
    except BaseException:
        # Includes worker termination/timeout exceptions.  Because the
        # running claim was committed before translation began, rolling this
        # transaction back leaves the fenced running row recoverable by lease.
        session_db.rollback()
        raise
    finally:
        session_db.close()


def execute_translation_job(job_id: str) -> dict[str, Any]:
    """Worker entrypoint — runs a translation job end-to-end.

    Returns a small summary suitable for RQ's ``result`` and logging.
    Errors are captured onto the suggestion row and then re-raised so
    RQ marks the job as failed too.
    """
    suggestion = get_suggestion_by_job_id(job_id)
    if suggestion is None:
        logger.warning("translation suggestion row missing for job %s", job_id)
        return {"job_id": job_id, "status": "missing"}

    if suggestion.status not in {"queued", "running"}:
        # Already processed — nothing to do. This can happen if RQ
        # retries a completed job.
        logger.info(
            "translation job %s already in state %s; skipping",
            job_id,
            suggestion.status,
        )
        return {"job_id": job_id, "status": suggestion.status}

    worker_token = uuid.uuid4().hex
    if not mark_running(job_id, worker_token=worker_token):
        current = get_suggestion_by_job_id(job_id)
        current_status = current.status if current is not None else "missing"
        logger.info(
            "translation job %s lost its worker claim to state %s",
            job_id,
            current_status,
        )
        return {"job_id": job_id, "status": current_status}

    scope = suggestion.scope
    source_title = suggestion.source_title or ""
    source_description = suggestion.source_description or ""

    try:
        try:
            translated_title, translated_description = _translate_with(
                get_translator_backend(), scope, source_title, source_description
            )
        except TranslatorUnavailableError as exc:
            # The configured backend cannot serve at all — an exhausted
            # balance, a revoked key. That is not this product's problem and
            # not something the operator can fix from the edit screen, so
            # finish the job with the offline translator instead of failing.
            fallback = get_fallback_translator_backend()
            if fallback is None:
                raise
            logger.warning(
                "translation job %s falling back to %s: %s", job_id, fallback.name, exc
            )
            translated_title, translated_description = _translate_with(
                fallback, scope, source_title, source_description
            )

        stored = _persist_translation_result(
            job_id=job_id,
            worker_token=worker_token,
            translated_title=translated_title,
            translated_description=translated_description,
            auto_apply=bool(suggestion.auto_apply),
        )

    except TranslationError as exc:
        logger.exception("translation job %s failed", job_id)
        mark_failed(job_id, worker_token=worker_token, error_message=_describe_failure(exc))
        raise
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.exception("translation job %s failed unexpectedly", job_id)
        mark_failed(
            job_id,
            worker_token=worker_token,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    current = get_suggestion_by_job_id(job_id)
    return {
        "job_id": job_id,
        "status": current.status if current is not None else "missing",
        "scope": scope,
    }
