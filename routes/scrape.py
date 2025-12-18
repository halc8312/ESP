"""
Scraping routes.
"""
import traceback
from urllib.parse import urlencode
from flask import Blueprint, render_template, request
from flask_login import login_required, current_user

from mercari_db import scrape_search_result, scrape_single_item
import yahoo_db
from services.product_service import save_scraped_items_to_db

scrape_bp = Blueprint('scrape', __name__)


@scrape_bp.route("/scrape", methods=["GET", "POST"])
@login_required
def scrape_form():
    return render_template("scrape_form.html")


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
            # site="yahoo" can be passed if we want to distinguish in DB, but Product model "site" field is used.
            # save_scraped_items_to_db defaults to "mercari". Let's check its signature.
            new_count, updated_count = save_scraped_items_to_db(items, site="yahoo", user_id=current_user.id)
        else:
            # Default to Mercari
            items = scrape_single_item(target_url, headless=True)
            new_count, updated_count = save_scraped_items_to_db(items, site="mercari", user_id=current_user.id)
        
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
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="yahoo")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"Yahoo Search Error: {str(e)}"

        else:
            # Mercari Search Logic (Existing)
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
                new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="mercari")
            except Exception as e:
                traceback.print_exc()
                items = []
                new_count = updated_count = 0
                error_msg = f"Mercari Search Error: {str(e)}"

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
    )
