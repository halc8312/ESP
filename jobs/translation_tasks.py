"""
Worker-callable translation task execution.

A translation job:

1. Loads the ``TranslationSuggestion`` row identified by ``job_id`` and
   flips its status to ``running``.
2. Uses the configured :class:`TranslatorBackend` to translate the
   captured source title / description (plain text for the title, HTML
   segmentation for the description so rich-text structure survives).
3. Writes the result back to the suggestion row as ``succeeded`` (or
   ``failed`` with an error message) and lets the web UI poll the API
   to surface the result for review.

The actual ``Product`` row is never updated directly; the operator has
to explicitly apply the suggestion from the UI. This matches the "必要
に応じて実行・確認できる形が望ましい" requirement from the DM.
"""
from __future__ import annotations

import logging
from typing import Any

from services.rich_text import normalize_rich_text
from services.translator import get_translator_backend
from services.translator.base import TranslationError
from services.translator.suggestion_store import (
    get_suggestion_by_job_id,
    mark_failed,
    mark_running,
    mark_succeeded,
)


logger = logging.getLogger("jobs.translation_tasks")


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

    mark_running(job_id)

    scope = suggestion.scope
    source_title = suggestion.source_title or ""
    source_description = suggestion.source_description or ""

    try:
        backend = get_translator_backend()
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

        mark_succeeded(
            job_id,
            translated_title=translated_title,
            translated_description=translated_description,
        )
    except TranslationError as exc:
        logger.exception("translation job %s failed", job_id)
        mark_failed(job_id, error_message=str(exc))
        raise
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.exception("translation job %s failed unexpectedly", job_id)
        mark_failed(job_id, error_message=f"{type(exc).__name__}: {exc}")
        raise

    return {
        "job_id": job_id,
        "status": "succeeded",
        "scope": scope,
    }
