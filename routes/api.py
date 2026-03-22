"""
API routes for scraping job status polling and lightweight product updates.
"""
from datetime import datetime
from flask import Blueprint, jsonify, request, url_for
from flask_login import login_required, current_user

from database import SessionLocal
from models import Product
from services.queue_backend import get_queue_backend, serialize_scrape_job_for_api

api_bp = Blueprint('api', __name__, url_prefix='/api')

# Backward-compatible alias for tests that monkeypatch routes.api.get_queue.
get_queue = get_queue_backend


def _parse_bulk_price_payload(mode, value, extra_value):
    if mode == "reset":
        return {"mode": mode}

    if mode == "fixed":
        try:
            fixed_value = int(value)
        except (TypeError, ValueError):
            raise ValueError("fixed value must be an integer")
        if fixed_value < 0:
            raise ValueError("fixed value must be >= 0")
        return {"mode": mode, "value": fixed_value}

    if mode == "fixed_add":
        try:
            delta = int(value)
        except (TypeError, ValueError):
            raise ValueError("fixed_add value must be an integer")
        return {"mode": mode, "value": delta}

    if mode in {"margin", "margin_plus_fixed"}:
        try:
            margin = float(value)
        except (TypeError, ValueError):
            raise ValueError("margin must be a number")
        if margin < 0 or margin >= 100:
            raise ValueError("margin must satisfy 0 <= margin < 100")

        payload = {"mode": mode, "value": margin}
        if mode == "margin_plus_fixed":
            try:
                payload["extra_value"] = int(extra_value)
            except (TypeError, ValueError):
                raise ValueError("margin_plus_fixed extra_value must be an integer")
        return payload

    raise ValueError("Unsupported mode")


def _calculate_bulk_price(product, parsed):
    mode = parsed["mode"]
    if mode == "reset":
        return None

    if mode == "fixed":
        return parsed["value"]

    cost = product.last_price
    if cost is None:
        raise ValueError("cost price is missing")

    if mode == "fixed_add":
        return cost + parsed["value"]

    if mode == "margin":
        return int(round(cost / (1 - parsed["value"] / 100)))

    if mode == "margin_plus_fixed":
        margin_price = cost / (1 - parsed["value"] / 100)
        return int(round(margin_price + parsed["extra_value"]))

    raise ValueError("Unsupported mode")


@api_bp.route("/scrape/status/<job_id>")
@login_required
def get_scrape_status(job_id):
    """
    スクレイピングジョブのステータスをJSONで返す。
    フロントエンドがポーリングに使用する。

    Response:
        {
            "job_id": "...",
            "status": "queued" | "running" | "completed" | "failed",
            "result": {...} | null,
            "error": "..." | null,
            "elapsed_seconds": 12.3,
            "queue_position": 2 | null
        }
    """
    queue = get_queue()
    status = queue.get_status(job_id, user_id=current_user.id)

    if status is None:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(serialize_scrape_job_for_api(status))


@api_bp.route("/scrape/jobs")
@login_required
def get_scrape_jobs():
    """ログインユーザーの最近の抽出ジョブ一覧を返す。"""
    raw_limit = request.args.get("limit", "10")
    limit = int(raw_limit) if raw_limit.isdigit() else 10
    limit = max(1, min(limit, 10))

    queue = get_queue()
    jobs = queue.get_jobs_for_user(
        user_id=current_user.id,
        limit=limit,
        include_terminal=True,
    )
    return jsonify({"jobs": [serialize_scrape_job_for_api(job) for job in jobs]})


@api_bp.route("/products/<int:product_id>/inline-update", methods=["PATCH"])
@login_required
def inline_update_product(product_id):
    """
    商品一覧のインライン編集用エンドポイント。

    Request JSON:
        {
            "field": "selling_price" | "custom_title_en",
            "value": ...
        }
    """
    payload = request.get_json(silent=True) or {}
    field = payload.get("field")
    value = payload.get("value")

    allowed_fields = {"selling_price", "custom_title_en"}
    if field not in allowed_fields:
        return jsonify({"error": "Unsupported field"}), 400

    session_db = SessionLocal()
    try:
        product = session_db.query(Product).filter_by(id=product_id, user_id=current_user.id).one_or_none()
        if not product:
            return jsonify({"error": "Product not found"}), 404

        if field == "selling_price":
            if value in (None, ""):
                normalized_value = None
            else:
                try:
                    normalized_value = int(value)
                except (TypeError, ValueError):
                    return jsonify({"error": "selling_price must be an integer"}), 400
                if normalized_value < 0:
                    return jsonify({"error": "selling_price must be >= 0"}), 400
        else:
            normalized_value = str(value).strip() if value is not None else ""
            normalized_value = normalized_value or None

        setattr(product, field, normalized_value)
        session_db.commit()

        return jsonify(
            {
                "ok": True,
                "product_id": product.id,
                "field": field,
                "value": normalized_value,
            }
        )
    finally:
        session_db.close()


@api_bp.route("/products/bulk-price", methods=["POST"])
@login_required
def bulk_price_update():
    """
    商品一覧の選択商品に対する一括価格設定。

    Request JSON:
        {
            "ids": [1, 2, 3],
            "mode": "margin" | "fixed_add" | "fixed" | "margin_plus_fixed" | "reset",
            "value": ...,
            "extra_value": ...
        }
    """
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("ids") or []
    mode = payload.get("mode")
    value = payload.get("value")
    extra_value = payload.get("extra_value")

    try:
        product_ids = [int(pid) for pid in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be an array of integers"}), 400

    if not product_ids:
        return jsonify({"error": "No products selected"}), 400

    try:
        parsed = _parse_bulk_price_payload(mode, value, extra_value)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    session_db = SessionLocal()
    try:
        products = (
            session_db.query(Product)
            .filter(Product.id.in_(product_ids), Product.user_id == current_user.id)
            .all()
        )
        if not products:
            return jsonify({"error": "No products found"}), 404

        updated_products = []
        skipped_products = []

        for product in products:
            try:
                new_price = _calculate_bulk_price(product, parsed)
            except ValueError as exc:
                skipped_products.append({"id": product.id, "reason": str(exc)})
                continue

            product.selling_price = new_price
            product.updated_at = datetime.utcnow()
            updated_products.append({"id": product.id, "selling_price": new_price})

        session_db.commit()

        return jsonify(
            {
                "ok": True,
                "mode": parsed["mode"],
                "updated_count": len(updated_products),
                "skipped_count": len(skipped_products),
                "updated_products": updated_products,
                "skipped_products": skipped_products,
            }
        )
    finally:
        session_db.close()
