"""
Scraping routes.
"""
from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for
from flask_login import login_required, current_user

from database import SessionLocal
from jobs.scrape_tasks import execute_scrape_job
from models import Shop
from services.product_service import save_scraped_items_to_db
from services.queue_backend import get_queue_backend
from services.scrape_request import (
    build_scrape_job_context,
    build_scrape_task_request,
    detect_site_from_url,
)


# Backward-compatible alias for tests that monkeypatch routes.scrape.get_queue.
get_queue = get_queue_backend


scrape_bp = Blueprint('scrape', __name__)


def _build_scrape_task(site, target_url, keyword, price_min, price_max, sort, category, limit, user_id, persist_to_db=True, shop_id=None):
    request_payload = build_scrape_task_request(
        site=site,
        target_url=target_url,
        keyword=keyword,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
        category=category,
        limit=limit,
        user_id=user_id,
        persist_to_db=persist_to_db,
        shop_id=shop_id,
    )
    return lambda: execute_scrape_job(request_payload)


@scrape_bp.route("/scrape", methods=["GET", "POST"])
@login_required
def scrape_form():
    session_db = SessionLocal()
    try:
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        return render_template(
            "scrape_form.html",
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@scrape_bp.route("/scrape/run", methods=["POST"])
@login_required
def scrape_run():
    target_url = request.form.get("target_url")
    keyword = request.form.get("keyword", "")
    price_min = request.form.get("price_min")
    price_max = request.form.get("price_max")
    sort = request.form.get("sort", "created_desc")
    category = request.form.get("category")
    limit_str = request.form.get("limit", "10")
    limit = int(limit_str) if limit_str.isdigit() else 10
    response_mode = request.form.get("response_mode", "").strip().lower()
    preview_mode = response_mode == "preview"
    current_shop_id = session.get('current_shop_id')

    # URL から site を推定する（キュー振り分けに使用）
    if target_url:
        site = detect_site_from_url(target_url)
    else:
        site = request.form.get("site", "mercari")

    task_request = build_scrape_task_request(
        site=site,
        target_url=target_url,
        keyword=keyword,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
        category=category,
        limit=limit,
        user_id=current_user.id,
        persist_to_db=not preview_mode,
        shop_id=current_shop_id,
    )
    task_fn = _build_scrape_task(
        site=site,
        target_url=target_url,
        keyword=keyword,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
        category=category,
        limit=limit,
        user_id=current_user.id,
        persist_to_db=not preview_mode,
        shop_id=current_shop_id,
    )
    job_context = build_scrape_job_context(
        site=site,
        target_url=target_url,
        keyword=keyword,
        limit=limit,
        persist_to_db=not preview_mode,
    )

    queue = get_queue()
    job_id = queue.enqueue(
        site=site,
        task_fn=task_fn,
        user_id=current_user.id,
        context=job_context,
        request_payload=task_request,
        mode="preview" if preview_mode else "persist",
    )

    if preview_mode:
        return jsonify(
            {
                "job_id": job_id,
                "status": "queued",
                "context": job_context,
                "status_url": url_for('api.get_scrape_status', job_id=job_id),
                "register_url": url_for('scrape.register_selected'),
                "result_url": url_for('scrape.scrape_form', job_id=job_id),
                "elapsed_seconds": 0,
                "queue_position": None,
            }
        ), 202

    return redirect(url_for('scrape.scrape_status', job_id=job_id))


@scrape_bp.route("/scrape/status/<job_id>")
@login_required
def scrape_status(job_id):
    """スクレイピング待機ページ（ポーリング用）"""
    session_db = SessionLocal()
    try:
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        return render_template(
            "scrape_waiting.html",
            job_id=job_id,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@scrape_bp.route("/scrape/result/<job_id>")
@login_required
def scrape_result(job_id):
    """スクレイピング完了後の結果表示ページ"""
    queue = get_queue()
    status = queue.get_status(job_id, user_id=current_user.id)

    session_db = SessionLocal()
    try:
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        if status is None:
            return render_template(
                "scrape_result.html",
                search_url="",
                keyword="",
                price_min=None,
                price_max=None,
                sort="",
                category=None,
                limit=10,
                items=[],
                new_count=0,
                updated_count=0,
                excluded_count=0,
                error_msg="ジョブが見つかりません（期限切れの可能性があります）",
                all_shops=all_shops,
                current_shop_id=current_shop_id,
            )

        if status["status"] == "failed":
            return render_template(
                "scrape_result.html",
                search_url="",
                keyword="",
                price_min=None,
                price_max=None,
                sort="",
                category=None,
                limit=10,
                items=[],
                new_count=0,
                updated_count=0,
                excluded_count=0,
                error_msg=status.get("error") or "商品抽出中にエラーが発生しました",
                all_shops=all_shops,
                current_shop_id=current_shop_id,
            )

        if status["status"] != "completed":
            # まだ完了していない場合は待機ページへ戻す
            return redirect(url_for('scrape.scrape_status', job_id=job_id))

        result = status.get("result") or {}
        return render_template(
            "scrape_result.html",
            search_url=result.get("search_url", ""),
            keyword=result.get("keyword", ""),
            price_min=result.get("price_min"),
            price_max=result.get("price_max"),
            sort=result.get("sort", ""),
            category=result.get("category"),
            limit=result.get("limit", 10),
            items=result.get("items", []),
            new_count=result.get("new_count", 0),
            updated_count=result.get("updated_count", 0),
            excluded_count=result.get("excluded_count", 0),
            error_msg=result.get("error_msg", ""),
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@scrape_bp.route("/scrape/register-selected", methods=["POST"])
@login_required
def register_selected():
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("job_id")
    raw_indices = payload.get("selected_indices") or []

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    try:
        selected_indices = [int(idx) for idx in raw_indices]
    except (TypeError, ValueError):
        return jsonify({"error": "selected_indices must be integers"}), 400

    if not selected_indices:
        return jsonify({"error": "No items selected"}), 400

    queue = get_queue()
    status = queue.get_status(job_id, user_id=current_user.id)
    if status is None:
        return jsonify({"error": "Job not found"}), 404
    if status["status"] != "completed":
        return jsonify({"error": "Job is not completed yet"}), 409

    result = status.get("result") or {}
    items = result.get("items") or []
    selected_items = []
    seen = set()

    for idx in selected_indices:
        if idx in seen:
            continue
        if idx < 0 or idx >= len(items):
            continue
        seen.add(idx)
        selected_items.append(items[idx])

    if not selected_items:
        return jsonify({"error": "No valid items selected"}), 400

    new_count, updated_count = save_scraped_items_to_db(
        selected_items,
        user_id=current_user.id,
        site=result.get("site", "mercari"),
        shop_id=result.get("shop_id"),
    )

    return jsonify(
        {
            "ok": True,
            "registered_count": len(selected_items),
            "new_count": new_count,
            "updated_count": updated_count,
        }
    )


