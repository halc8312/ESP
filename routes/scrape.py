"""
Scraping routes.
"""
from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for
from flask_login import login_required, current_user

import uuid

from database import SessionLocal
from jobs.scrape_tasks import execute_scrape_job
from models import PriceList, PriceListItem, Shop
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
        price_lists_query = session_db.query(PriceList).filter(
            PriceList.user_id == current_user.id,
        )
        if current_shop_id:
            price_lists_query = price_lists_query.filter(PriceList.shop_id == current_shop_id)
        price_lists = price_lists_query.order_by(PriceList.created_at.desc()).all()
        return render_template(
            "scrape_form.html",
            all_shops=all_shops,
            current_shop_id=current_shop_id,
            price_lists=price_lists,
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


def _resolve_selected_items(payload):
    """job_id と selected_indices を検証し、(selected_items, result, error_response) を返す。"""
    job_id = payload.get("job_id")
    raw_indices = payload.get("selected_indices") or []

    if not job_id:
        return None, None, (jsonify({"error": "job_id is required"}), 400)

    try:
        selected_indices = [int(idx) for idx in raw_indices]
    except (TypeError, ValueError):
        return None, None, (jsonify({"error": "selected_indices must be integers"}), 400)

    if not selected_indices:
        return None, None, (jsonify({"error": "No items selected"}), 400)

    queue = get_queue()
    status = queue.get_status(job_id, user_id=current_user.id)
    if status is None:
        return None, None, (jsonify({"error": "Job not found"}), 404)
    if status["status"] != "completed":
        return None, None, (jsonify({"error": "Job is not completed yet"}), 409)

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
        return None, None, (jsonify({"error": "No valid items selected"}), 400)

    return selected_items, result, None


@scrape_bp.route("/scrape/register-selected", methods=["POST"])
@login_required
def register_selected():
    payload = request.get_json(silent=True) or {}
    selected_items, result, error_response = _resolve_selected_items(payload)
    if error_response is not None:
        return error_response

    try:
        save_summary = save_scraped_items_to_db(
            selected_items,
            user_id=current_user.id,
            site=result.get("site", "mercari"),
            shop_id=result.get("shop_id"),
            manual_selection=True,
            return_summary=True,
            raise_on_error=True,
        )
    except Exception:
        return jsonify({"error": "選択商品の保存に失敗しました。時間をおいて再度お試しください。"}), 500

    registered_count = int(save_summary.get("processed_count") or 0)
    if registered_count <= 0:
        return jsonify(
            {
                "error": "選択した商品は保存できませんでした。商品URL、タイトル、取得状態を確認してください。",
                "registered_count": 0,
                "rejected_count": save_summary.get("rejected_count", len(selected_items)),
            }
        ), 422

    return jsonify(
        {
            "ok": True,
            "registered_count": registered_count,
            "selected_count": len(selected_items),
            "new_count": save_summary.get("new_count", 0),
            "updated_count": save_summary.get("updated_count", 0),
            "rejected_count": save_summary.get("rejected_count", 0),
        }
    )


@scrape_bp.route("/scrape/register-to-pricelist", methods=["POST"])
@login_required
def register_to_pricelist():
    payload = request.get_json(silent=True) or {}
    raw_price_list_id = payload.get("price_list_id")
    new_list_name = (payload.get("new_list_name") or "").strip()

    if not raw_price_list_id and not new_list_name:
        return jsonify({"error": "登録先の商品リストを選択するか、新しいリスト名を入力してください。"}), 400

    price_list_id = None
    if raw_price_list_id:
        try:
            price_list_id = int(raw_price_list_id)
        except (TypeError, ValueError):
            return jsonify({"error": "price_list_id must be an integer"}), 400

    selected_items, result, error_response = _resolve_selected_items(payload)
    if error_response is not None:
        return error_response

    try:
        save_summary = save_scraped_items_to_db(
            selected_items,
            user_id=current_user.id,
            site=result.get("site", "mercari"),
            shop_id=result.get("shop_id"),
            manual_selection=True,
            return_summary=True,
            raise_on_error=True,
            is_listed=False,
        )
    except Exception:
        return jsonify({"error": "選択商品の保存に失敗しました。時間をおいて再度お試しください。"}), 500

    registered_count = int(save_summary.get("processed_count") or 0)
    product_ids = save_summary.get("product_ids") or []
    if registered_count <= 0 or not product_ids:
        return jsonify(
            {
                "error": "選択した商品は保存できませんでした。商品URL、タイトル、取得状態を確認してください。",
                "registered_count": 0,
                "rejected_count": save_summary.get("rejected_count", len(selected_items)),
            }
        ), 422

    session_db = SessionLocal()
    try:
        current_shop_id = session.get('current_shop_id')
        if price_list_id is not None:
            price_list = session_db.query(PriceList).filter(
                PriceList.id == price_list_id,
                PriceList.user_id == current_user.id,
            ).first()
            if price_list is None:
                return jsonify({"error": "指定された商品リストが見つかりません。"}), 404
        else:
            price_list = PriceList(
                user_id=current_user.id,
                shop_id=current_shop_id,
                name=new_list_name,
                token=str(uuid.uuid4()),
            )
            session_db.add(price_list)
            session_db.flush()

        existing_product_ids = {
            item.product_id
            for item in session_db.query(PriceListItem.product_id).filter(
                PriceListItem.price_list_id == price_list.id
            )
        }
        max_order = (
            session_db.query(PriceListItem)
            .filter(PriceListItem.price_list_id == price_list.id)
            .count()
        )
        added_count = 0
        for product_id in product_ids:
            if product_id in existing_product_ids:
                continue
            session_db.add(
                PriceListItem(
                    price_list_id=price_list.id,
                    product_id=product_id,
                    sort_order=max_order + added_count,
                )
            )
            added_count += 1

        session_db.commit()
        price_list_name = price_list.name
        price_list_id_value = price_list.id
    except Exception:
        session_db.rollback()
        return jsonify({"error": "商品リストへの登録に失敗しました。時間をおいて再度お試しください。"}), 500
    finally:
        session_db.close()

    return jsonify(
        {
            "ok": True,
            "registered_count": registered_count,
            "selected_count": len(selected_items),
            "new_count": save_summary.get("new_count", 0),
            "updated_count": save_summary.get("updated_count", 0),
            "rejected_count": save_summary.get("rejected_count", 0),
            "added_to_list_count": added_count,
            "price_list_id": price_list_id_value,
            "price_list_name": price_list_name,
            "price_list_url": url_for('pricelist.pricelist_items', pricelist_id=price_list_id_value),
        }
    )


