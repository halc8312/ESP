"""
Scraping routes.
"""
import traceback
from urllib.parse import urlencode
from flask import Blueprint, render_template, request, session
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


scrape_bp = Blueprint('scrape', __name__)


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

    search_url = ""
    items = []
    new_count = 0
    updated_count = 0
    error_msg = ""

    if target_url:
        # Check domain for switching scrapers
        if "shopping.yahoo.co.jp" in target_url:
            items = yahoo_db.scrape_single_item(target_url, headless=True)
            site = "yahoo"
        elif "fril.jp" in target_url:
            # Rakuma (ラクマ)
            items = rakuma_db.scrape_single_item(target_url, headless=True)
            site = "rakuma"
        elif "suruga-ya.jp" in target_url:
            # 駿河屋
            items = surugaya_db.scrape_single_item(target_url, headless=True)
            site = "surugaya"
        elif "netmall.hardoff.co.jp" in target_url:
            # オフモール
            items = offmall_db.scrape_single_item(target_url, headless=True)
            site = "offmall"
        elif "auctions.yahoo.co.jp" in target_url:
            # ヤフオク
            items = yahuoku_db.scrape_single_item(target_url, headless=True)
            site = "yahuoku"
        elif "snkrdunk.com" in target_url:
            # SNKRDUNK
            items = snkrdunk_db.scrape_single_item(target_url, headless=True)
            site = "snkrdunk"
        else:
            # Default to Mercari
            items = scrape_single_item(target_url, headless=True)
            site = "mercari"
        
        # Apply exclusion filter
        items, excluded = filter_excluded_items(items, current_user.id)
        
        new_count, updated_count = save_scraped_items_to_db(items, site=site, user_id=current_user.id)

        
    else: # This block handles search results
        params = {}
        if keyword: params["keyword"] = keyword
        if price_min: params["price_min"] = price_min
        if price_max: params["price_max"] = price_max
        if sort: params["sort"] = sort
        if category: params["category_id"] = category

        # Determine site (default mercari)
        site = request.form.get("site", "mercari")
        
        if site == "yahoo":
            # Yahoo Search Logic
            # Yahoo Shopping Search URL: https://shopping.yahoo.co.jp/search?p={keyword} (Simple version)
            # Ignoring price/sort for Beta version as implementation differs
            
            base = "https://shopping.yahoo.co.jp/search?"
            # Yahoo uses 'p' for keyword
            y_params = {"p": keyword} if keyword else {}
            # If no keyword, Yahoo search might fail or show top. 
            
            search_url = base + urlencode(y_params)
            
            try:
                items = yahoo_db.scrape_search_result(
                    search_url=search_url,
                    max_items=limit,
                    max_scroll=3,
                    headless=True,
                )
                # Apply exclusion filter
                items, excluded = filter_excluded_items(items, current_user.id)
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="yahoo")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"Yahoo Search Error: {str(e)}"

        elif site == "rakuma":
            # Rakuma Search Logic
            # Rakuma Search URL: https://fril.jp/s?query={keyword}
            
            base = "https://fril.jp/s?"
            r_params = {"query": keyword} if keyword else {}
            
            search_url = base + urlencode(r_params)
            
            try:
                items = rakuma_db.scrape_search_result(
                    search_url=search_url,
                    max_items=limit,
                    max_scroll=3,
                    headless=True,
                )
                # Apply exclusion filter
                items, excluded = filter_excluded_items(items, current_user.id)
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="rakuma")
            except Exception as e:
                traceback.print_exc()
                items = []

                new_count = updated_count = 0
                error_msg = f"Rakuma Search Error: {str(e)}"

        elif site == "surugaya":
            # 駿河屋 Search
            base = "https://www.suruga-ya.jp/search?"
            s_params = {"search_word": keyword} if keyword else {}
            if keyword:
                s_params["is_stock"] = "1"  # In stock only
            
            search_url = base + urlencode(s_params)
            
            try:
                items = surugaya_db.scrape_search_result(
                    search_url=search_url,
                    max_items=limit,
                    max_scroll=3,
                    headless=True,
                )
                items, excluded = filter_excluded_items(items, current_user.id)
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="surugaya")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"Surugaya Search Error: {str(e)}"

        elif site == "offmall":
            # オフモール Search
            base = "https://netmall.hardoff.co.jp/search?"
            o_params = {"q": keyword} if keyword else {}
            
            search_url = base + urlencode(o_params)
            
            try:
                items = offmall_db.scrape_search_result(
                    search_url=search_url,
                    max_items=limit,
                    max_scroll=3,
                    headless=True,
                )
                items, excluded = filter_excluded_items(items, current_user.id)
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="offmall")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"Offmall Search Error: {str(e)}"

        elif site == "yahuoku":
            # ヤフオク Search
            base = "https://auctions.yahoo.co.jp/search/search?"
            y_params = {"p": keyword} if keyword else {}
            
            search_url = base + urlencode(y_params)
            
            try:
                items = yahuoku_db.scrape_search_result(
                    search_url=search_url,
                    max_items=limit,
                    max_scroll=3,
                    headless=True,
                )
                items, excluded = filter_excluded_items(items, current_user.id)
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="yahuoku")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"Yahoo Auctions Search Error: {str(e)}"

        elif site == "snkrdunk":
            # SNKRDUNK Search
            base = "https://snkrdunk.com/search?"
            s_params = {"keywords": keyword} if keyword else {}
            
            search_url = base + urlencode(s_params)
            
            try:
                items = snkrdunk_db.scrape_search_result(
                    search_url=search_url,
                    max_items=limit,
                    max_scroll=3,
                    headless=True,
                )
                items, excluded = filter_excluded_items(items, current_user.id)
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="snkrdunk")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"SNKRDUNK Search Error: {str(e)}"

        else:
            # Mercari Search Logic (Default)
            base = "https://jp.mercari.com/search?"
            query = urlencode(params)
            search_url = base + query

            try:
                items = scrape_search_result(
                    search_url=search_url,
                    max_items=limit,
                    max_scroll=3,
                    headless=True,
                )
                # Apply exclusion filter
                items, excluded = filter_excluded_items(items, current_user.id)
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="mercari")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"Mercari Search Error: {str(e)}"


    # Get shop data for template
    session_db = SessionLocal()
    try:
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        
        return render_template(
            "scrape_result.html",
            search_url=search_url,
            keyword=keyword,
            price_min=price_min,
            price_max=price_max,
            sort=sort,
            category=category,
            limit=limit,
            items=items,
            new_count=new_count,
            updated_count=updated_count,
            error_msg=error_msg,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    finally:
        session_db.close()
