"""
API routes for scraping job status polling and lightweight product updates.
"""
import math

from flask import Blueprint, jsonify, request, url_for
from flask_login import login_required, current_user

from database import SessionLocal
from models import PricingRule, Product, Variant
from services.pricing_service import (
    calculate_configured_product_price,
    calculate_selling_price,
)
from services.queue_backend import get_queue_backend, serialize_scrape_job_for_api
from services.scrape_job_store import dismiss_job_record, dismiss_job_records, list_dismissed_job_ids_for_user
from time_utils import utc_now

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
        if not math.isfinite(margin) or margin < 0 or margin > 500:
            raise ValueError("margin must satisfy 0 <= margin <= 500")

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
    if mode == "fixed":
        return parsed["value"]

    cost = product.last_price
    if cost is None:
        raise ValueError("cost price is missing")

    if mode == "fixed_add":
        result = cost + parsed["value"]
        if result < 0:
            raise ValueError("calculated price must be >= 0")
        return result

    if mode == "margin":
        return int(round(cost * (1 + parsed["value"] / 100)))

    if mode == "margin_plus_fixed":
        margin_price = cost * (1 + parsed["value"] / 100)
        result = int(round(margin_price + parsed["extra_value"]))
        if result < 0:
            raise ValueError("calculated price must be >= 0")
        return result

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
    fetch_limit = min(30, max(limit, limit * 3))

    queue = get_queue()
    jobs = queue.get_jobs_for_user(
        user_id=current_user.id,
        limit=fetch_limit,
        include_terminal=True,
    )
    dismissed_job_ids = list_dismissed_job_ids_for_user(current_user.id, limit=50)
    visible_jobs = [
        job for job in jobs
        if not (
            str(job.get("status") or "") in {"completed", "failed"}
            and str(job.get("job_id") or "") in dismissed_job_ids
        )
    ]
    return jsonify({"jobs": [serialize_scrape_job_for_api(job) for job in visible_jobs[:limit]]})


@api_bp.route("/scrape/jobs/<job_id>/dismiss", methods=["POST"])
@login_required
def dismiss_scrape_job(job_id):
    """小窓トラッカーから閉じたジョブを再表示しないよう記録する。"""
    dismissed = dismiss_job_record(job_id, current_user.id)
    if not dismissed:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"ok": True, "job_id": job_id})


@api_bp.route("/scrape/jobs/dismiss-batch", methods=["POST"])
@login_required
def dismiss_scrape_jobs():
    """小窓トラッカー上の完了済みジョブをまとめて閉じる。"""
    payload = request.get_json(silent=True) or {}
    raw_job_ids = payload.get("job_ids")
    if not isinstance(raw_job_ids, list):
        return jsonify({"error": "job_ids must be an array"}), 400

    dismissed_count = dismiss_job_records(raw_job_ids[:50], current_user.id)
    return jsonify({"ok": True, "dismissed_count": dismissed_count})


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

        if field == "custom_title_en":
            if normalized_value != product.custom_title_en:
                product.custom_title_en_source_hash = None
            # This endpoint is an explicit operator edit, including when the
            # submitted value is empty.
            product.custom_title_en_manually_edited = True
        else:
            # A product-level manual price intentionally restores the common
            # default contract.  Remove older per-variant overrides so the
            # value shown in the list is also the value exports resolve from.
            session_db.query(Variant).filter_by(
                product_id=product.id
            ).update(
                {Variant.selling_price: None},
                synchronize_session=False,
            )
        if field == "selling_price" and normalized_value is None:
            normalized_value = calculate_configured_product_price(
                session_db,
                product,
                user_id=current_user.id,
            )
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
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@api_bp.route("/products/<int:product_id>/recalc-price", methods=["POST"])
@login_required
def recalc_product_price(product_id):
    """
    商品編集画面の「個別の利益率/送料」プレビュー計算用エンドポイント。

    Request JSON:
        {
            "pricing_rule_id": int | null,
            "manual_margin_rate": int | null,
            "manual_shipping_cost": int | null
        }

    Returns the recalculated selling_price using the rule currently selected
    in the edit form (or the persisted product rule for older callers),
    overridden by the supplied manual values. Does NOT persist anything to
    the database — frontend uses this to populate variant price inputs as a
    preview only.
    """
    payload = request.get_json(silent=True) or {}

    def _coerce_optional_int(raw, field_name):
        if raw is None or raw == "":
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{field_name} must be an integer")
        if value < 0:
            raise ValueError(f"{field_name} must be >= 0")
        return value

    try:
        manual_margin = _coerce_optional_int(payload.get("manual_margin_rate"), "manual_margin_rate")
        manual_shipping = _coerce_optional_int(payload.get("manual_shipping_cost"), "manual_shipping_cost")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    session_db = SessionLocal()
    try:
        product = (
            session_db.query(Product)
            .filter_by(id=product_id, user_id=current_user.id)
            .one_or_none()
        )
        if not product:
            return jsonify({"success": False, "error": "Product not found"}), 404

        if product.last_price is None or product.last_price <= 0:
            return jsonify({"success": False, "error": "仕入価格が未取得のため計算できません"}), 400

        selected_rule_id = product.pricing_rule_id
        if "pricing_rule_id" in payload:
            raw_rule_id = payload.get("pricing_rule_id")
            if raw_rule_id in (None, ""):
                selected_rule_id = None
            else:
                try:
                    selected_rule_id = int(raw_rule_id)
                except (TypeError, ValueError):
                    return jsonify(
                        {
                            "success": False,
                            "error": "価格ルールを選び直してください",
                        }
                    ), 400
                if selected_rule_id <= 0:
                    return jsonify(
                        {
                            "success": False,
                            "error": "価格ルールを選び直してください",
                        }
                    ), 400

        rule = None
        if selected_rule_id:
            rule = (
                session_db.query(PricingRule)
                .filter_by(id=selected_rule_id, user_id=current_user.id)
                .one_or_none()
            )
            if rule is None:
                return jsonify(
                    {
                        "success": False,
                        "error": "選択した価格ルールを利用できません",
                    }
                ), 400

        selling_price = calculate_selling_price(
            product.last_price,
            rule,
            manual_margin_rate=manual_margin,
            manual_shipping_cost=manual_shipping,
        )

        return jsonify(
            {
                "success": True,
                "product_id": product.id,
                "cost_price": product.last_price,
                "selling_price": selling_price,
                "manual_margin_rate": manual_margin,
                "manual_shipping_cost": manual_shipping,
                "rule_id": rule.id if rule else None,
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

    if not isinstance(raw_ids, list):
        return jsonify({"error": "ids must be an array of integers"}), 400

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
                if parsed["mode"] == "reset":
                    new_price = calculate_configured_product_price(
                        session_db,
                        product,
                        user_id=current_user.id,
                    )
                else:
                    new_price = _calculate_bulk_price(product, parsed)
            except ValueError as exc:
                skipped_products.append({"id": product.id, "reason": str(exc)})
                continue

            product.selling_price = new_price
            # Bulk product-level pricing is an explicit replacement, not a
            # preview.  Clear row-level manual prices so the reported result
            # cannot disagree with subsequent exports.
            session_db.query(Variant).filter_by(
                product_id=product.id
            ).update(
                {Variant.selling_price: None},
                synchronize_session=False,
            )
            product.updated_at = utc_now()
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
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
