"""
Translation API routes.

Endpoints:

* ``POST /api/products/<product_id>/translate`` — enqueue a new
  translation suggestion for the given product.
* ``GET /api/products/<product_id>/translation-suggestions`` — list
  the most recent suggestions along with current status.
* ``POST /api/translation-suggestions/<job_id>/apply`` — copy the
  translated fields onto the product and record the source hashes so
  the UI can show a "source updated" badge later.
* ``POST /api/translation-suggestions/<job_id>/reject`` — mark a
  suggestion as rejected so it's no longer surfaced to the operator.

The routes are designed so that the ``inmemory`` queue backend
(used in tests and local dev) runs the translator synchronously and
returns a ready-to-review suggestion in the same request, while the
``rq`` backend queues the job to the media queue and the UI polls
for completion.
"""
from __future__ import annotations

import logging
import os
import uuid

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from database import SessionLocal
from models import Product, TranslationSuggestion
from services.media_queue import (
    enqueue_media_job,
    resolve_media_queue_name,
    resolve_queue_backend_name,
)
from services.translator import compute_source_hash
from services.translator.suggestion_store import (
    create_suggestion,
    get_suggestion_by_job_id,
    list_suggestions_for_product,
    mark_terminal_state,
    serialize_suggestion,
    serialize_suggestions,
)
from time_utils import utc_now


logger = logging.getLogger("routes.translation")

translation_bp = Blueprint("translation", __name__, url_prefix="/api")


_ALLOWED_SCOPES = {"title", "description", "full"}


def _load_owned_product(session_db, product_id: int) -> Product | None:
    product = (
        session_db.query(Product)
        .filter(Product.id == product_id, Product.user_id == current_user.id)
        .one_or_none()
    )
    return product


def _resolve_source_for_scope_safe(
    session_db,
    product: Product,
    scope: str,
) -> tuple[str, str]:
    """Pick the Japanese title / description the operator sees in the editor.

    Falls back to the latest scraped snapshot description so a product
    can be translated even before the operator has edited it.
    """
    from models import ProductSnapshot

    title = ""
    description = ""

    if scope in {"title", "full"}:
        title = (product.custom_title or product.last_title or "").strip()

    if scope in {"description", "full"}:
        description = (product.custom_description or "").strip()
        if not description:
            snap = (
                session_db.query(ProductSnapshot)
                .filter_by(product_id=product.id)
                .order_by(ProductSnapshot.scraped_at.desc())
                .first()
            )
            if snap is not None and snap.description:
                description = str(snap.description).strip()

    return title, description


@translation_bp.route("/products/<int:product_id>/translate", methods=["POST"])
@login_required
def enqueue_translation(product_id: int):
    payload = request.get_json(silent=True) or {}
    scope = str(payload.get("scope") or "full").strip().lower()
    if scope not in _ALLOWED_SCOPES:
        return jsonify({"error": "invalid_scope", "allowed": sorted(_ALLOWED_SCOPES)}), 400

    session_db = SessionLocal()
    try:
        product = _load_owned_product(session_db, product_id)
        if product is None:
            return jsonify({"error": "not_found"}), 404

        source_title, source_description = _resolve_source_for_scope_safe(
            session_db, product, scope
        )

        if scope == "title" and not source_title:
            return jsonify({"error": "empty_source", "field": "title"}), 400
        if scope == "description" and not source_description:
            return jsonify({"error": "empty_source", "field": "description"}), 400
        if scope == "full" and not source_title and not source_description:
            return jsonify({"error": "empty_source"}), 400

        provider = str(
            current_app.config.get("TRANSLATOR_BACKEND")
            or os.environ.get("TRANSLATOR_BACKEND")
            or "argos"
        ).lower()

        job_id = str(uuid.uuid4())
        suggestion = create_suggestion(
            session=session_db,
            job_id=job_id,
            product_id=product.id,
            user_id=current_user.id,
            scope=scope,
            provider=provider,
            source_title=source_title or None,
            source_description=source_description or None,
            source_title_hash=compute_source_hash(source_title) or None,
            source_description_hash=compute_source_hash(source_description) or None,
        )
        session_db.commit()

        backend_name = resolve_queue_backend_name()
        enqueue_error: str | None = None
        if backend_name == "rq":
            try:
                enqueue_media_job(
                    job_id=job_id,
                    func="jobs.translation_tasks.execute_translation_job",
                    args=(job_id,),
                    description=f"translate product {product.id} scope={scope}",
                )
            except Exception as exc:
                logger.exception(
                    "failed to enqueue translation job %s for product %s",
                    job_id,
                    product.id,
                )
                enqueue_error = str(exc)
                # Fall back to synchronous execution so the UI still gets a result.
                _run_translation_inline(job_id)
        else:
            _run_translation_inline(job_id)

        # Reload the suggestion from the DB so the returned payload reflects
        # any status transitions performed by the inline run.
        refreshed = get_suggestion_by_job_id(job_id, user_id=current_user.id)
        serialized = serialize_suggestion(refreshed) if refreshed else serialize_suggestion(suggestion)
        if enqueue_error:
            serialized["enqueue_fallback"] = enqueue_error

        return (
            jsonify(
                {
                    "job_id": job_id,
                    "queue": resolve_media_queue_name(),
                    "backend": backend_name,
                    "suggestion": serialized,
                }
            ),
            202 if backend_name == "rq" and not enqueue_error else 201,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


def _run_translation_inline(job_id: str) -> None:
    """Run a translation job in-process. Used for tests and local dev."""
    try:
        from jobs.translation_tasks import execute_translation_job

        execute_translation_job(job_id)
    except Exception:  # pragma: no cover - logged by the task itself
        logger.exception("inline translation job %s failed", job_id)


@translation_bp.route(
    "/products/<int:product_id>/translation-suggestions",
    methods=["GET"],
)
@login_required
def list_translation_suggestions(product_id: int):
    session_db = SessionLocal()
    try:
        product = _load_owned_product(session_db, product_id)
        if product is None:
            return jsonify({"error": "not_found"}), 404

        limit = request.args.get("limit", type=int) or 5
        rows = list_suggestions_for_product(
            product.id,
            user_id=current_user.id,
            limit=max(1, min(limit, 20)),
            session=session_db,
        )
        return jsonify({"items": serialize_suggestions(rows)})
    finally:
        session_db.close()


@translation_bp.route(
    "/translation-suggestions/<job_id>/apply",
    methods=["POST"],
)
@login_required
def apply_translation_suggestion(job_id: str):
    payload = request.get_json(silent=True) or {}
    apply_title = bool(payload.get("apply_title", True))
    apply_description = bool(payload.get("apply_description", True))

    session_db = SessionLocal()
    try:
        suggestion = (
            session_db.query(TranslationSuggestion)
            .filter_by(job_id=job_id, user_id=current_user.id)
            .one_or_none()
        )
        if suggestion is None:
            return jsonify({"error": "not_found"}), 404
        if suggestion.status != "succeeded":
            return (
                jsonify(
                    {
                        "error": "suggestion_not_ready",
                        "status": suggestion.status,
                    }
                ),
                409,
            )

        product = (
            session_db.query(Product)
            .filter_by(id=suggestion.product_id, user_id=current_user.id)
            .one_or_none()
        )
        if product is None:
            return jsonify({"error": "product_not_found"}), 404

        changes: dict[str, str | None] = {}

        if apply_title and suggestion.translated_title:
            product.custom_title_en = suggestion.translated_title
            product.custom_title_en_source_hash = suggestion.source_title_hash
            changes["custom_title_en"] = suggestion.translated_title

        if apply_description and suggestion.translated_description:
            from services.rich_text import normalize_rich_text

            sanitised_description = (
                normalize_rich_text(suggestion.translated_description) or None
            )
            if sanitised_description:
                product.custom_description_en = sanitised_description
                product.custom_description_en_source_hash = (
                    suggestion.source_description_hash
                )
                changes["custom_description_en"] = sanitised_description

        if not changes:
            return (
                jsonify({"error": "nothing_to_apply"}),
                400,
            )

        suggestion.status = "applied"
        suggestion.updated_at = utc_now()
        session_db.commit()

        return jsonify(
            {
                "status": "applied",
                "product_id": product.id,
                "applied_fields": sorted(changes.keys()),
                "suggestion": serialize_suggestion(suggestion),
            }
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@translation_bp.route(
    "/translation-suggestions/<job_id>/reject",
    methods=["POST"],
)
@login_required
def reject_translation_suggestion(job_id: str):
    session_db = SessionLocal()
    try:
        suggestion = (
            session_db.query(TranslationSuggestion)
            .filter_by(job_id=job_id, user_id=current_user.id)
            .one_or_none()
        )
        if suggestion is None:
            return jsonify({"error": "not_found"}), 404
        if suggestion.status in {"applied", "rejected"}:
            return jsonify({"status": suggestion.status, "suggestion": serialize_suggestion(suggestion)})

        mark_terminal_state(job_id, status="rejected", session=session_db)
        session_db.commit()
        refreshed = (
            session_db.query(TranslationSuggestion)
            .filter_by(job_id=job_id)
            .one_or_none()
        )
        return jsonify(
            {
                "status": "rejected",
                "suggestion": serialize_suggestion(refreshed) if refreshed else None,
            }
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
