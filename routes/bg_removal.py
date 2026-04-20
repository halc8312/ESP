"""
Background-removal API routes.

Endpoints:

* ``POST /api/products/<product_id>/images/remove-background`` — enqueue
  a new bg-removal job for the given source image URL. The URL must be
  one currently associated with the product (scraped snapshot or
  operator-uploaded ``/media/product_images/...``).
* ``GET /api/products/<product_id>/image-processing-jobs`` — list the
  most recent jobs for a product along with current status.
* ``POST /api/image-processing-jobs/<job_id>/apply`` — replace the
  original image URL with the processed one in the product's image
  list (persisted into a new ``ProductSnapshot`` row).
* ``POST /api/image-processing-jobs/<job_id>/reject`` — mark a job as
  rejected so it's no longer surfaced for review.
* ``POST /internal/bg-removal/<job_id>/upload`` — HMAC-authenticated
  endpoint called by the media worker to hand back processed PNG
  bytes. Bypasses the user session but verifies the shared secret.

The routes are designed so that the ``inmemory`` queue backend (tests
and local dev) runs the worker task synchronously, while the ``rq``
backend queues the job to the media queue and the UI polls for
completion.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Iterable

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from database import SessionLocal
from models import ImageProcessingJob, Product, ProductSnapshot
from services.bg_remover.internal_auth import (
    JOB_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify_signature,
)
from services.bg_remover.job_store import (
    create_job,
    get_job_by_job_id,
    list_jobs_for_product,
    mark_succeeded,
    mark_terminal_state,
    serialize_job,
    serialize_jobs,
)
from services.image_service import IMAGE_STORAGE_PATH
from services.media_queue import (
    enqueue_media_job,
    resolve_media_queue_name,
    resolve_queue_backend_name,
)
from time_utils import utc_now


logger = logging.getLogger("routes.bg_removal")

bg_removal_bp = Blueprint("bg_removal", __name__)


PROCESSED_IMAGE_SUBDIR = "processed_images"
PROCESSED_IMAGE_URL_PREFIX = f"/media/{PROCESSED_IMAGE_SUBDIR}/"


def _load_owned_product(session_db, product_id: int) -> Product | None:
    return (
        session_db.query(Product)
        .filter(Product.id == product_id, Product.user_id == current_user.id)
        .one_or_none()
    )


def _iter_current_image_urls(session_db, product: Product) -> list[str]:
    """Return the operator's current image list for ``product``.

    The image list is stored as a pipe-separated string in the latest
    ``ProductSnapshot`` row; we treat that as the authoritative list
    for ownership checks.
    """
    snap = (
        session_db.query(ProductSnapshot)
        .filter_by(product_id=product.id)
        .order_by(ProductSnapshot.scraped_at.desc())
        .first()
    )
    if not snap or not snap.image_urls:
        return []
    return [url.strip() for url in snap.image_urls.split("|") if url.strip()]


def _is_allowed_source_image_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return (
        lower.startswith("http://")
        or lower.startswith("https://")
        or url.startswith("/")
    )


def _processed_image_dir() -> str:
    path = os.path.join(IMAGE_STORAGE_PATH, PROCESSED_IMAGE_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _persist_processed_image(*, job_id: str, content: bytes) -> str:
    filename = f"bg_{job_id}.png"
    dest_path = os.path.join(_processed_image_dir(), filename)
    with open(dest_path, "wb") as fh:
        fh.write(content)
    return f"{PROCESSED_IMAGE_URL_PREFIX}{filename}"


def _replace_image_in_list(
    urls: Iterable[str], *, old_url: str, new_url: str
) -> tuple[list[str], bool]:
    updated = []
    replaced = False
    for url in urls:
        if url == old_url and not replaced:
            updated.append(new_url)
            replaced = True
        else:
            updated.append(url)
    return updated, replaced


def _build_snapshot_with_images(
    session_db,
    product: Product,
    new_image_urls: list[str],
) -> ProductSnapshot | None:
    """Write a new ProductSnapshot carrying the updated image list."""
    latest = (
        session_db.query(ProductSnapshot)
        .filter_by(product_id=product.id)
        .order_by(ProductSnapshot.scraped_at.desc())
        .first()
    )
    snapshot = ProductSnapshot(
        product_id=product.id,
        scraped_at=utc_now(),
        title=getattr(latest, "title", None) or product.last_title,
        price=getattr(latest, "price", None) or product.last_price,
        status=getattr(latest, "status", None) or product.last_status,
        description=getattr(latest, "description", None),
        image_urls="|".join(new_image_urls),
    )
    session_db.add(snapshot)
    return snapshot


def _run_bg_removal_inline(job_id: str) -> None:
    """Execute a bg-removal job in-process (tests / inmemory backend).

    The synchronous path calls the backend directly and writes the
    result to the shared image storage without going through the HTTP
    upload path. This lets tests replace the backend via
    ``set_bg_remover_backend_for_tests`` and still exercise the full
    apply/reject flow.
    """
    from services.bg_remover import get_bg_remover_backend
    from services.bg_remover.base import BackgroundRemovalError
    from services.bg_remover.job_store import (
        get_job_by_job_id as _fetch,
        mark_failed,
        mark_running,
    )

    job = _fetch(job_id)
    if job is None:
        logger.warning("inline bg job row missing for %s", job_id)
        return
    if job.status not in {"queued", "running"}:
        return

    mark_running(job_id)

    source_url = job.source_image_url
    try:
        # Attempt to resolve a managed ``/media/...`` path locally first;
        # otherwise fetch via HTTP.
        source_bytes = _load_source_bytes_for_inline(source_url)
        backend = get_bg_remover_backend()
        result_bytes = backend.remove_background(source_bytes)
        if not result_bytes:
            raise BackgroundRemovalError("backend returned empty bytes")
        result_url = _persist_processed_image(
            job_id=job_id, content=result_bytes
        )
        mark_succeeded(job_id, result_image_url=result_url)
    except BackgroundRemovalError as exc:
        logger.exception("inline bg-removal job %s failed", job_id)
        mark_failed(job_id, error_message=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("inline bg-removal job %s failed unexpectedly", job_id)
        mark_failed(job_id, error_message=f"{type(exc).__name__}: {exc}")


def _load_source_bytes_for_inline(source_url: str) -> bytes:
    """Load source image bytes for the inline (non-worker) execution path."""
    from services.bg_remover.base import BackgroundRemovalError

    if source_url.startswith("/"):
        relative = source_url.lstrip("/")
        if relative.startswith("media/"):
            relative = relative[len("media/"):]
        candidate = os.path.abspath(os.path.join(IMAGE_STORAGE_PATH, relative))
        storage_root = os.path.abspath(IMAGE_STORAGE_PATH)
        try:
            if os.path.commonpath([candidate, storage_root]) != storage_root:
                raise BackgroundRemovalError("source URL outside storage root")
        except ValueError as exc:
            raise BackgroundRemovalError("invalid source URL") from exc
        if not os.path.exists(candidate):
            raise BackgroundRemovalError(
                f"source image not found on disk: {source_url}"
            )
        with open(candidate, "rb") as fh:
            return fh.read()

    if source_url.startswith(("http://", "https://")):
        import requests

        try:
            response = requests.get(source_url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            raise BackgroundRemovalError(
                f"failed to download source image: {exc}"
            ) from exc

    raise BackgroundRemovalError(
        f"unsupported source_image_url scheme: {source_url!r}"
    )


@bg_removal_bp.route(
    "/api/products/<int:product_id>/images/remove-background",
    methods=["POST"],
)
@login_required
def enqueue_bg_removal(product_id: int):
    payload = request.get_json(silent=True) or {}
    image_url = str(payload.get("image_url") or "").strip()
    if not _is_allowed_source_image_url(image_url):
        return jsonify({"error": "invalid_image_url"}), 400

    session_db = SessionLocal()
    try:
        product = _load_owned_product(session_db, product_id)
        if product is None:
            return jsonify({"error": "not_found"}), 404

        current_images = _iter_current_image_urls(session_db, product)
        if image_url not in current_images:
            return jsonify({"error": "image_url_not_associated"}), 400

        provider = str(
            current_app.config.get("BG_REMOVAL_BACKEND")
            or os.environ.get("BG_REMOVAL_BACKEND")
            or "rembg"
        ).lower()

        job_id = str(uuid.uuid4())
        job = create_job(
            session=session_db,
            job_id=job_id,
            product_id=product.id,
            user_id=current_user.id,
            source_image_url=image_url,
            provider=provider,
        )
        session_db.commit()

        backend_name = resolve_queue_backend_name()
        enqueue_error: str | None = None
        if backend_name == "rq":
            try:
                enqueue_media_job(
                    job_id=job_id,
                    func="jobs.bg_removal_tasks.execute_bg_removal_job",
                    args=(job_id,),
                    description=f"bg-remove product {product.id}",
                )
            except Exception as exc:
                logger.exception(
                    "failed to enqueue bg-removal job %s for product %s",
                    job_id,
                    product.id,
                )
                enqueue_error = str(exc)
                _run_bg_removal_inline(job_id)
        else:
            _run_bg_removal_inline(job_id)

        refreshed = get_job_by_job_id(job_id, user_id=current_user.id)
        serialized = serialize_job(refreshed) if refreshed else serialize_job(job)
        if enqueue_error:
            serialized["enqueue_fallback"] = enqueue_error

        return (
            jsonify(
                {
                    "job_id": job_id,
                    "queue": resolve_media_queue_name(),
                    "backend": backend_name,
                    "job": serialized,
                }
            ),
            202 if backend_name == "rq" and not enqueue_error else 201,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@bg_removal_bp.route(
    "/api/products/<int:product_id>/image-processing-jobs",
    methods=["GET"],
)
@login_required
def list_image_jobs(product_id: int):
    session_db = SessionLocal()
    try:
        product = _load_owned_product(session_db, product_id)
        if product is None:
            return jsonify({"error": "not_found"}), 404

        limit = request.args.get("limit", type=int) or 20
        rows = list_jobs_for_product(
            product.id,
            user_id=current_user.id,
            limit=max(1, min(limit, 100)),
            session=session_db,
        )
        return jsonify({"items": serialize_jobs(rows)})
    finally:
        session_db.close()


@bg_removal_bp.route(
    "/api/image-processing-jobs/<job_id>",
    methods=["GET"],
)
@login_required
def get_image_job(job_id: str):
    row = get_job_by_job_id(job_id, user_id=current_user.id)
    if row is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"job": serialize_job(row)})


@bg_removal_bp.route(
    "/api/image-processing-jobs/<job_id>/apply",
    methods=["POST"],
)
@login_required
def apply_image_job(job_id: str):
    session_db = SessionLocal()
    try:
        job = (
            session_db.query(ImageProcessingJob)
            .filter_by(job_id=job_id, user_id=current_user.id)
            .one_or_none()
        )
        if job is None:
            return jsonify({"error": "not_found"}), 404
        if job.status != "succeeded" or not job.result_image_url:
            return (
                jsonify({"error": "job_not_ready", "status": job.status}),
                409,
            )

        product = (
            session_db.query(Product)
            .filter_by(id=job.product_id, user_id=current_user.id)
            .one_or_none()
        )
        if product is None:
            return jsonify({"error": "product_not_found"}), 404

        current_images = _iter_current_image_urls(session_db, product)
        if job.source_image_url not in current_images:
            return (
                jsonify({"error": "source_image_no_longer_present"}),
                409,
            )

        updated, replaced = _replace_image_in_list(
            current_images,
            old_url=job.source_image_url,
            new_url=job.result_image_url,
        )
        if not replaced:
            return (
                jsonify({"error": "source_image_no_longer_present"}),
                409,
            )

        _build_snapshot_with_images(session_db, product, updated)
        job.status = "applied"
        job.updated_at = utc_now()
        session_db.commit()

        return jsonify(
            {
                "job": serialize_job(job),
                "images": updated,
            }
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@bg_removal_bp.route(
    "/api/image-processing-jobs/<job_id>/reject",
    methods=["POST"],
)
@login_required
def reject_image_job(job_id: str):
    session_db = SessionLocal()
    try:
        row = (
            session_db.query(ImageProcessingJob)
            .filter_by(job_id=job_id, user_id=current_user.id)
            .one_or_none()
        )
        if row is None:
            return jsonify({"error": "not_found"}), 404

        row.status = "rejected"
        row.updated_at = utc_now()
        session_db.commit()
        return jsonify({"job": serialize_job(row)})
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@bg_removal_bp.route(
    "/internal/bg-removal/<job_id>/upload",
    methods=["POST"],
)
def internal_upload_bg_result(job_id: str):
    """HMAC-authenticated endpoint the worker uses to upload result bytes.

    The handler deliberately does not go through ``login_required`` and
    instead verifies the shared-secret signature against the raw
    request body.
    """
    raw = request.get_data(cache=False) or b""
    signature = request.headers.get(SIGNATURE_HEADER)
    timestamp = request.headers.get(TIMESTAMP_HEADER)
    header_job_id = request.headers.get(JOB_ID_HEADER)

    if not verify_signature(
        job_id=header_job_id or job_id,
        timestamp=timestamp,
        body=raw,
        signature=signature,
    ):
        logger.warning(
            "rejecting internal bg-removal upload with bad signature for job %s",
            job_id,
        )
        return jsonify({"error": "invalid_signature"}), 401

    if header_job_id and header_job_id != job_id:
        return jsonify({"error": "job_id_mismatch"}), 400

    if not raw:
        return jsonify({"error": "empty_body"}), 400

    row = get_job_by_job_id(job_id)
    if row is None:
        return jsonify({"error": "not_found"}), 404
    if row.status in {"applied", "rejected"}:
        return (
            jsonify({"error": "job_already_terminal", "status": row.status}),
            409,
        )

    result_url = _persist_processed_image(job_id=job_id, content=raw)
    updated = mark_succeeded(job_id, result_image_url=result_url)
    if updated is None:
        return jsonify({"error": "update_failed"}), 500

    return jsonify({"result_image_url": result_url, "job": serialize_job(updated)})
