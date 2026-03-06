"""
Scraping routes.
"""
import traceback
from urllib.parse import urlencode
from flask import Blueprint, render_template, request, session, redirect, url_for
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
from services.filter_service import filter_excluded_items
from services.scrape_queue import get_queue


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


def _detect_site_from_url(url: str) -> str:
    """URL からサイト識別子を推定する。一致しない場合は "mercari" を返す。"""
    for domain, site in _DOMAIN_SITE_MAP:
        if domain in url:
            return site
    return "mercari"


def _build_scrape_task(site, target_url, keyword, price_min, price_max, sort, category, limit, user_id):
    """
    スクレイピングタスク関数を構築して返す。
    バックグラウンドスレッドで実行される。
    Flask コンテキストに依存しない。
    """
    def task():
        items = []
        new_count = 0
        updated_count = 0
        error_msg = ""
        search_url = ""

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
                items = scraper_fn(target_url, headless=True)

                items, _ = filter_excluded_items(items, user_id)
                new_count, updated_count = save_scraped_items_to_db(items, site=_site, user_id=user_id)

            else:
                params = {}
                if keyword:
                    params["keyword"] = keyword
                if price_min:
                    params["price_min"] = price_min
                if price_max:
                    params["price_max"] = price_max
                if sort:
                    params["sort"] = sort
                if category:
                    params["category_id"] = category

                if site == "yahoo":
                    base = "https://shopping.yahoo.co.jp/search?"
                    y_params = {"p": keyword} if keyword else {}
                    search_url = base + urlencode(y_params)
                    items = yahoo_db.scrape_search_result(
                        search_url=search_url,
                        max_items=limit,
                        max_scroll=3,
                        headless=True,
                    )
                    items, _ = filter_excluded_items(items, user_id)
                    new_count, updated_count = save_scraped_items_to_db(items, user_id=user_id, site="yahoo")

                elif site == "rakuma":
                    base = "https://fril.jp/s?"
                    r_params = {"query": keyword} if keyword else {}
                    search_url = base + urlencode(r_params)
                    items = rakuma_db.scrape_search_result(
                        search_url=search_url,
                        max_items=limit,
                        max_scroll=3,
                        headless=True,
                    )
                    items, _ = filter_excluded_items(items, user_id)
                    new_count, updated_count = save_scraped_items_to_db(items, user_id=user_id, site="rakuma")

                elif site == "surugaya":
                    base = "https://www.suruga-ya.jp/search?"
                    s_params = {"search_word": keyword} if keyword else {}
                    if keyword:
                        s_params["is_stock"] = "1"
                    search_url = base + urlencode(s_params)
                    items = surugaya_db.scrape_search_result(
                        search_url=search_url,
                        max_items=limit,
                        max_scroll=3,
                        headless=True,
                    )
                    items, _ = filter_excluded_items(items, user_id)
                    new_count, updated_count = save_scraped_items_to_db(items, user_id=user_id, site="surugaya")

                elif site == "offmall":
                    base = "https://netmall.hardoff.co.jp/search?"
                    o_params = {"q": keyword} if keyword else {}
                    search_url = base + urlencode(o_params)
                    items = offmall_db.scrape_search_result(
                        search_url=search_url,
                        max_items=limit,
                        max_scroll=3,
                        headless=True,
                    )
                    items, _ = filter_excluded_items(items, user_id)
                    new_count, updated_count = save_scraped_items_to_db(items, user_id=user_id, site="offmall")

                elif site == "yahuoku":
                    base = "https://auctions.yahoo.co.jp/search/search?"
                    y_params = {"p": keyword} if keyword else {}
                    search_url = base + urlencode(y_params)
                    items = yahuoku_db.scrape_search_result(
                        search_url=search_url,
                        max_items=limit,
                        max_scroll=3,
                        headless=True,
                    )
                    items, _ = filter_excluded_items(items, user_id)
                    new_count, updated_count = save_scraped_items_to_db(items, user_id=user_id, site="yahuoku")

                elif site == "snkrdunk":
                    base = "https://snkrdunk.com/search?"
                    s_params = {"keywords": keyword} if keyword else {}
                    search_url = base + urlencode(s_params)
                    items = snkrdunk_db.scrape_search_result(
                        search_url=search_url,
                        max_items=limit,
                        max_scroll=3,
                        headless=True,
                    )
                    items, _ = filter_excluded_items(items, user_id)
                    new_count, updated_count = save_scraped_items_to_db(items, user_id=user_id, site="snkrdunk")

                else:
                    # Mercari (default)
                    base = "https://jp.mercari.com/search?"
                    search_url = base + urlencode(params)
                    items = scrape_search_result(
                        search_url=search_url,
                        max_items=limit,
                        max_scroll=3,
                        headless=True,
                    )
                    items, _ = filter_excluded_items(items, user_id)
                    new_count, updated_count = save_scraped_items_to_db(items, user_id=user_id, site="mercari")

        except Exception as e:
            traceback.print_exc()
            error_msg = str(e)
            items = []
            new_count = updated_count = 0

        return {
            "items": items,
            "new_count": new_count,
            "updated_count": updated_count,
            "error_msg": error_msg,
            "search_url": search_url,
            "keyword": keyword or "",
            "price_min": price_min,
            "price_max": price_max,
            "sort": sort or "",
            "category": category,
            "limit": limit,
            "site": site,
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
    )

    queue = get_queue()
    job_id = queue.enqueue(
        site=site,
        task_fn=task_fn,
        user_id=current_user.id,
    )

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
    status = queue.get_status(job_id)

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
                error_msg=status.get("error") or "スクレイピング中にエラーが発生しました",
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
            error_msg=result.get("error_msg", ""),
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    finally:
        session_db.close()
