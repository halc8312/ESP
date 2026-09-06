"""Public inquiry submission and an owner-scoped, read-only-by-default inbox."""
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from database import SessionLocal
from models import CatalogRequest, Shop
from services.catalog_request_service import CatalogRequestError, submit_catalog_request
from services.rate_limit_service import get_client_ip
from time_utils import utc_now

catalog_requests_bp = Blueprint("catalog_requests", __name__)


@catalog_requests_bp.post("/catalog/<token>/requests")
def submit_request(token):
    if request.content_length and request.content_length > 32_768:
        return jsonify(error="The request is too large.", code="validation_error"), 413
    session_db = SessionLocal()
    try:
        payload, status = submit_catalog_request(
            session_db, token, request.get_json(silent=True), get_client_ip(request),
        )
        return jsonify(payload), status
    except CatalogRequestError as error:
        session_db.rollback()
        response = jsonify(error.payload)
        if error.status == 429:
            response.headers["Retry-After"] = "900"
        return response, error.status
    except SQLAlchemyError as error:
        session_db.rollback()
        # Do not log SQL parameters containing customer contact details.
        current_app.logger.warning("Catalog inquiry persistence failed (%s)", type(error).__name__)
        return jsonify(
            error="Your request could not be saved. Please try again shortly.", code="service_unavailable",
        ), 503
    finally:
        session_db.close()


def _navigation_context(session_db):
    return {
        "all_shops": session_db.query(Shop).filter_by(user_id=current_user.id).all(),
        "current_shop_id": session.get("current_shop_id"),
    }


@catalog_requests_bp.get("/requests")
@login_required
def request_list():
    session_db = SessionLocal()
    try:
        query = session_db.query(CatalogRequest).filter_by(user_id=current_user.id)
        unread_count = query.filter(CatalogRequest.viewed_at.is_(None)).count()
        filter_mode = "all" if request.args.get("filter") == "all" else "unread"
        if filter_mode == "unread":
            query = query.filter(CatalogRequest.viewed_at.is_(None))
        total = query.count()
        per_page = 25
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(pages, max(1, request.args.get("page", 1, type=int)))
        rows = (
            query.options(selectinload(CatalogRequest.items))
            .order_by(CatalogRequest.created_at.desc(), CatalogRequest.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all()
        )
        return render_template(
            "requests.html", catalog_requests=rows, filter_mode=filter_mode,
            page=page, per_page=per_page, total=total, pages=pages, unread_count=unread_count,
            **_navigation_context(session_db),
        )
    finally:
        session_db.close()


@catalog_requests_bp.get("/requests/<int:request_id>")
@login_required
def request_detail(request_id):
    session_db = SessionLocal()
    try:
        catalog_request = session_db.query(CatalogRequest).options(
            selectinload(CatalogRequest.items),
        ).filter_by(id=request_id, user_id=current_user.id).first()
        if catalog_request is None:
            abort(404)
        return render_template(
            "request_detail.html", catalog_request=catalog_request, **_navigation_context(session_db),
        )
    finally:
        session_db.close()


@catalog_requests_bp.post("/requests/<int:request_id>/read")
@login_required
def mark_read(request_id):
    session_db = SessionLocal()
    try:
        catalog_request = session_db.query(CatalogRequest).filter_by(
            id=request_id, user_id=current_user.id,
        ).first()
        if catalog_request is None:
            abort(404)
        if catalog_request.viewed_at is None:
            session_db.query(CatalogRequest).filter(
                CatalogRequest.id == request_id,
                CatalogRequest.user_id == current_user.id,
                CatalogRequest.viewed_at.is_(None),
            ).update({"viewed_at": utc_now()}, synchronize_session=False)
            session_db.commit()
        return redirect(url_for("catalog_requests.request_detail", request_id=request_id))
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
