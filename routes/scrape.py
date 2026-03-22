"""
Scraping routes.
"""
import math
import traceback
from urllib.parse import urlencode, urlparse
from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for
from flask_login import login_required, current_user

from database import SessionLocal
from models import Shop
from mercari_db import scrape_search_result, scrape_single_item
import yahoo_db
import rakuma_db
import surugaya_db
import offmall_db
import yahuoku_db
import snkrdunk_db
from services.product_service import save_scraped_items_to_db
from services.filter_service import filter_excluded_items, filter_items_by_price, normalize_price_bounds
from services.queue_backend import get_queue_backend


# Backward-compatible alias for tests that monkeypatch routes.scrape.get_queue.
get_queue = get_queue_backend


scrape_bp = Blueprint('scrape', __name__)

# ドメインとサイト識別子のマッピング（URL からサイトを推定するために使用）
_DOMAIN_SITE_MAP = [
    ("fril.jp", "rakuma"),
    ("item.fril.jp", "rakuma"),
    ("jp.mercari.com", "mercari"),
    ("shopping.yahoo.co.jp", "yahoo"),
    ("suruga-ya.jp", "surugaya"),
    ("netmall.hardoff.co.jp", "offmall"),
    ("auctions.yahoo.co.jp", "yahuoku"),
    ("snkrdunk.com", "snkrdunk"),
]

_SITE_LABELS = {
    "mercari": "メルカリ",
    "yahoo": "Yahoo!ショッピング",
    "rakuma": "ラクマ",
    "surugaya": "駿河屋",
    "offmall": "オフモール",
    "yahuoku": "ヤフオク",
    "snkrdunk": "SNKRDUNK",
}


def _detect_site_from_url(url: str) -> str:
    """URL からサイト識別子を推定する。一致しない場合は "mercari" を返す。"""
    for domain, site in _DOMAIN_SITE_MAP:
        if domain in url:
            return site
    return "mercari"


_SEARCH_DEPTH_RULES = {
    "mercari": {"window": 16, "base": 2, "min": 3, "max": 10},
    "rakuma": {"window": 18, "base": 2, "min": 3, "max": 10},
    "yahoo": {"window": 24, "base": 2, "min": 3, "max": 8},
    "surugaya": {"window": 18, "base": 2, "min": 3, "max": 7},
    "offmall": {"window": 24, "base": 2, "min": 3, "max": 8},
    "yahuoku": {"window": 24, "base": 2, "min": 3, "max": 8},
    "snkrdunk": {"window": 20, "base": 2, "min": 3, "max": 8},
}


def _get_internal_search_limit(limit: int) -> int:
    """
    実際の表示件数とは別に、失敗や除外を吸収するための内部探索件数を返す。
    10件指定時の既存体感は維持しつつ、50/100件で探索量を増やす。
    """
    requested = max(1, int(limit or 10))
    if requested <= 10:
        return requested
    return min(150, max(requested + 10, int(math.ceil(requested * 1.4))))


def _get_search_depth(site: str, limit: int) -> int:
    """
    サイトごとに、要求件数に見合った探索深さ（ページ数/スクロール量）を返す。
    """
    requested = max(1, int(limit or 10))
    rule = _SEARCH_DEPTH_RULES.get(site, {"window": 20, "base": 2, "min": 3, "max": 8})
    depth = int(math.ceil(requested / rule["window"])) + rule["base"]
    return max(rule["min"], min(depth, rule["max"]))


def _simplify_target_label(target_url: str) -> str:
    if not target_url:
        return "URL指定"
    try:
        parsed = urlparse(target_url)
        return parsed.hostname or "URL指定"
    except ValueError:
        return "URL指定"


def _build_scrape_job_context(site, target_url, keyword, limit, persist_to_db):
    if target_url:
        return {
            "site_label": "URLから抽出",
            "detail_label": _simplify_target_label(target_url),
            "limit": 1,
            "limit_label": "1件",
            "persist_to_db": persist_to_db,
            "target_url": target_url,
            "keyword": keyword or "",
        }

    requested_limit = max(1, int(limit or 10))
    return {
        "site_label": _SITE_LABELS.get(site, "商品抽出"),
        "detail_label": f"キーワード: {keyword}" if keyword else "条件で抽出します",
        "limit": requested_limit,
        "limit_label": f"{requested_limit}件",
        "persist_to_db": persist_to_db,
        "target_url": "",
        "keyword": keyword or "",
    }


def _build_search_url(site, keyword, price_min, price_max, sort, category):
    """
    Build a site-aware search URL.

    Native price params are added where the current site exposes a verified URL
    contract. The common post-scrape price filter remains as defense-in-depth.
    """
    min_value, max_value = normalize_price_bounds(price_min, price_max)
    min_str = str(min_value) if min_value is not None else None
    max_str = str(max_value) if max_value is not None else None

    keyword = (keyword or "").strip()
    sort = (sort or "").strip()
    category = (category or "").strip()

    if site == "yahoo":
        params = {}
        if keyword:
            params["p"] = keyword
        if min_str:
            params["pf"] = min_str
        if max_str:
            params["pt"] = max_str
        return "https://shopping.yahoo.co.jp/search?" + urlencode(params)

    if site == "rakuma":
        params = {}
        if keyword:
            params["query"] = keyword
        if min_str:
            params["min"] = min_str
        if max_str:
            params["max"] = max_str
        return "https://fril.jp/s?" + urlencode(params)

    if site == "surugaya":
        params = {}
        if keyword:
            params["search_word"] = keyword
            params["is_stock"] = "1"
        if min_str or max_str:
            params["price"] = f"[{min_str or 0},{max_str or '*'}]"
        return "https://www.suruga-ya.jp/search?" + urlencode(params)

    if site == "offmall":
        params = {}
        if keyword:
            params["q"] = keyword
        if min_str:
            params["min"] = min_str
        if max_str:
            params["max"] = max_str
        return "https://netmall.hardoff.co.jp/search/?" + urlencode(params)

    if site == "yahuoku":
        params = {}
        if keyword:
            params["p"] = keyword
            params["va"] = keyword
        if min_str:
            params["aucminprice"] = min_str
        if max_str:
            params["aucmaxprice"] = max_str
        return "https://auctions.yahoo.co.jp/search/search?" + urlencode(params)

    if site == "snkrdunk":
        params = {}
        if keyword:
            params["keywords"] = keyword
        if min_str:
            params["minPrice"] = min_str
        if max_str:
            params["maxPrice"] = max_str
        return "https://snkrdunk.com/search?" + urlencode(params)

    params = {}
    if keyword:
        params["keyword"] = keyword
    if min_str:
        params["price_min"] = min_str
    if max_str:
        params["price_max"] = max_str
    if sort:
        params["sort"] = sort
    if category:
        params["category_id"] = category
    return "https://jp.mercari.com/search?" + urlencode(params)


def _build_scrape_task(site, target_url, keyword, price_min, price_max, sort, category, limit, user_id, persist_to_db=True, shop_id=None):
    """
    スクレイピングタスク関数を構築して返す。
    バックグラウンドスレッドで実行される。
    Flask コンテキストに依存しない。
    """
    def task():
        items = []
        new_count = 0
        updated_count = 0
        excluded_count = 0
        error_msg = ""
        search_url = ""
        normalized_price_min, normalized_price_max = normalize_price_bounds(price_min, price_max)

        def finalize(scraped_items, target_site):
            nonlocal items, excluded_count, new_count, updated_count
            filtered_items, excluded_count = filter_excluded_items(scraped_items, user_id)
            filtered_items, price_excluded_count = filter_items_by_price(
                filtered_items,
                price_min=normalized_price_min,
                price_max=normalized_price_max,
            )
            excluded_count += price_excluded_count
            items = filtered_items[:limit]
            if persist_to_db:
                new_count, updated_count = save_scraped_items_to_db(
                    items,
                    site=target_site,
                    user_id=user_id,
                    shop_id=shop_id,
                )

        try:
            if target_url:
                _site = _detect_site_from_url(target_url)
                scraper_map = {
                    "yahoo": yahoo_db.scrape_single_item,
                    "rakuma": rakuma_db.scrape_single_item,
                    "surugaya": surugaya_db.scrape_single_item,
                    "offmall": offmall_db.scrape_single_item,
                    "yahuoku": yahuoku_db.scrape_single_item,
                    "snkrdunk": snkrdunk_db.scrape_single_item,
                    "mercari": scrape_single_item,
                }
                scraper_fn = scraper_map.get(_site, scrape_single_item)
                finalize(scraper_fn(target_url, headless=True), _site)

            else:
                search_limit = _get_internal_search_limit(limit)
                search_depth = _get_search_depth(site, search_limit)
                search_url = _build_search_url(
                    site=site,
                    keyword=keyword,
                    price_min=normalized_price_min,
                    price_max=normalized_price_max,
                    sort=sort,
                    category=category,
                )

                if site == "yahoo":
                    items = yahoo_db.scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                    )
                    finalize(items, "yahoo")

                elif site == "rakuma":
                    items = rakuma_db.scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                    )
                    finalize(items, "rakuma")

                elif site == "surugaya":
                    items = surugaya_db.scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                    )
                    finalize(items, "surugaya")

                elif site == "offmall":
                    items = offmall_db.scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                    )
                    finalize(items, "offmall")

                elif site == "yahuoku":
                    items = yahuoku_db.scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                    )
                    finalize(items, "yahuoku")

                elif site == "snkrdunk":
                    items = snkrdunk_db.scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                    )
                    finalize(items, "snkrdunk")

                else:
                    # Mercari (default)
                    items = scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                    )
                    finalize(items, "mercari")

        except Exception as e:
            traceback.print_exc()
            error_msg = str(e)
            items = []
            new_count = updated_count = 0

        return {
            "items": items,
            "new_count": new_count,
            "updated_count": updated_count,
            "excluded_count": excluded_count,
            "error_msg": error_msg,
            "search_url": search_url,
            "keyword": keyword or "",
            "price_min": normalized_price_min,
            "price_max": normalized_price_max,
            "sort": sort or "",
            "category": category,
            "limit": limit,
            "site": site,
            "persist_to_db": persist_to_db,
            "shop_id": shop_id,
        }

    return task


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
        site = _detect_site_from_url(target_url)
    else:
        site = request.form.get("site", "mercari")

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
    job_context = _build_scrape_job_context(
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


