"""HMAC-only image ingress; no public source URLs or arbitrary filesystem paths."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from database import create_isolated_session
from models import Product, Shop
from services.product_image_delivery import (
    ImageDeliveryError, ImageSlotConflict, MAX_DELIVERY_BYTES,
    SIGNATURE_HEADER, TIMESTAMP_HEADER, canonical_delivery_bytes, persist_delivery_bytes,
    validate_delivery_bytes, verify_delivery_signature,
)
from time_utils import utc_now

product_images_bp = Blueprint("product_images", __name__)


def _authorize_detail_delivery(session, product_id, token):
    from services.product_detail_jobs import _owned_product_query, _scope_key
    from services.scrape_safety import UnsafeScrapeUrlError, validate_marketplace_url

    captured = session.query(Product).filter(
        Product.id == product_id, Product.detail_job_id == token,
        Product.detail_fetch_state == "running", Product.deleted_at.is_(None),
        Product.detail_source_url == Product.source_url,
        Product.detail_lease_expires_at > utc_now(),
    ).first()
    if captured is None or captured.detail_scope_key != _scope_key(captured.user_id, captured.shop_id):
        return None
    try:
        validate_marketplace_url(captured.source_url, captured.site, kind="detail")
    except (ValueError, UnsafeScrapeUrlError):
        return None
    if captured.shop_id is not None:
        shop_query = session.query(Shop).filter(Shop.id == captured.shop_id, Shop.user_id == captured.user_id)
        if shop_query.with_for_update().first() is None or not shop_query.update({Shop.name: Shop.name}, synchronize_session=False):
            return None
    query = _owned_product_query(session, product_id, captured.user_id).filter(
        Product.detail_job_id == token, Product.detail_fetch_state == "running",
        Product.source_url == captured.source_url, Product.detail_source_url == captured.source_url,
        Product.site == captured.site, Product.shop_id == captured.shop_id,
        Product.detail_scope_key == captured.detail_scope_key,
        Product.detail_lease_expires_at > utc_now(),
    )
    # UPDATE supplies a real SQLite write fence as well as PostgreSQL's row
    # lock. Source/shop/ownership edits must wait through file persistence.
    if query.with_for_update().first() is None or not query.update({Product.detail_job_id: token}, synchronize_session=False):
        return None
    return query


@product_images_bp.route("/internal/product-images/<kind>/<int:product_id>/<token>/<int:index>", methods=["POST"])
def internal_upload_product_image(kind, product_id, token, index):
    if request.content_length is not None and request.content_length > MAX_DELIVERY_BYTES:
        return jsonify({"error": "invalid_image"}), 413
    # Cap undeclared/chunked requests too; avoid loading an unbounded body.
    content = request.stream.read(MAX_DELIVERY_BYTES + 1)
    if len(content) > MAX_DELIVERY_BYTES:
        return jsonify({"error": "invalid_image"}), 413
    if not verify_delivery_signature(
        product_id=product_id, token=token, index=index, kind=kind, body=content,
        timestamp=request.headers.get(TIMESTAMP_HEADER), signature=request.headers.get(SIGNATURE_HEADER),
    ):
        return jsonify({"error": "unauthorized"}), 401
    try:
        ext = validate_delivery_bytes(content, request.content_type)
        content = canonical_delivery_bytes(content, ext)
    except (ImageDeliveryError, ValueError):
        return jsonify({"error": "invalid_image"}), 400
    session = create_isolated_session()
    created = None
    commit_attempted = False
    try:
        if kind == "detail":
            context = _authorize_detail_delivery(session, product_id, token)
        else:
            from services.product_thumbnail_jobs import authorize_thumbnail_delivery

            context = authorize_thumbnail_delivery(session, product_id, token)
        if context is None:
            return jsonify({"error": "stale_request"}), 409
        image_url, created = persist_delivery_bytes(product_id, token, index, content, ext, kind=kind)
        if kind == "detail":
            accepted = context.filter(Product.detail_lease_expires_at > utc_now()).update(
                {Product.detail_job_id: token}, synchronize_session=False,
            )
        else:
            from services.product_thumbnail_jobs import finalize_thumbnail_delivery

            accepted = finalize_thumbnail_delivery(session, context, image_url)
        if not accepted:
            if created is not None:
                created.unlink(missing_ok=True)
                created = None
            # Keep the write fence until cleanup: a retry must not accept a
            # file that this rejected request is still about to delete.
            session.rollback()
            return jsonify({"error": "stale_request"}), 409
        commit_attempted = True
        session.commit()
        return jsonify({"image_url": image_url})
    except ImageSlotConflict:
        session.rollback()
        return jsonify({"error": "slot_conflict"}), 409
    except Exception:
        if created is not None and not commit_attempted:
            created.unlink(missing_ok=True)
        # A commit error may mean the server committed but its acknowledgement
        # was lost. Retain the immutable file so retries cannot publish a 404.
        session.rollback()
        return jsonify({"error": "image_delivery_failed"}), 503
    finally:
        session.close()
