"""
Worker-callable scrape task execution.
"""
from __future__ import annotations

import logging
from typing import Any

import offmall_db
import rakuma_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db
from mercari_db import scrape_search_result, scrape_single_item
from services.filter_service import filter_excluded_items, filter_items_by_price, normalize_price_bounds
from services.product_service import save_scraped_items_to_db
from services.scrape_job_runtime import run_tracked_job
from services.scrape_request import (
    build_search_url,
    detect_site_from_url,
    get_internal_search_limit,
    get_search_depth,
)


logger = logging.getLogger("scrape_tasks")


def _get_smoke_result_payload(request_payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("__smoke_result", "_smoke_result", "smoke_result"):
        candidate = request_payload.get(key)
        if isinstance(candidate, dict):
            return candidate
    return None


def execute_scrape_job(request_payload: dict[str, Any]) -> dict[str, Any]:
    site = str(request_payload.get("site") or "mercari")
    target_url = str(request_payload.get("target_url") or "")
    keyword = str(request_payload.get("keyword") or "")
    price_min = request_payload.get("price_min")
    price_max = request_payload.get("price_max")
    sort = str(request_payload.get("sort") or "")
    category = request_payload.get("category")
    limit = max(1, int(request_payload.get("limit") or 10))
    user_id = request_payload.get("user_id")
    persist_to_db = bool(request_payload.get("persist_to_db", True))
    shop_id = request_payload.get("shop_id")

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
        smoke_result = _get_smoke_result_payload(request_payload)
        if smoke_result is not None:
            finalize(list(smoke_result.get("items") or []), str(smoke_result.get("site") or site))
            error_msg = str(smoke_result.get("error_msg") or "")
            search_url = str(smoke_result.get("search_url") or f"internal://stack-smoke/{site}")
            keyword = str(smoke_result.get("keyword") or keyword)
            sort = str(smoke_result.get("sort") or sort)
            category = smoke_result.get("category", category)
            return {
                "items": items,
                "new_count": new_count,
                "updated_count": updated_count,
                "excluded_count": excluded_count,
                "error_msg": error_msg,
                "search_url": search_url,
                "keyword": keyword,
                "price_min": normalized_price_min,
                "price_max": normalized_price_max,
                "sort": sort,
                "category": category,
                "limit": limit,
                "site": site,
                "persist_to_db": persist_to_db,
                "shop_id": shop_id,
            }

        if target_url:
            target_site = detect_site_from_url(target_url)
            scraper_map = {
                "yahoo": yahoo_db.scrape_single_item,
                "rakuma": rakuma_db.scrape_single_item,
                "surugaya": surugaya_db.scrape_single_item,
                "offmall": offmall_db.scrape_single_item,
                "yahuoku": yahuoku_db.scrape_single_item,
                "snkrdunk": snkrdunk_db.scrape_single_item,
                "mercari": scrape_single_item,
            }
            scraper_fn = scraper_map.get(target_site, scrape_single_item)
            finalize(scraper_fn(target_url, headless=True), target_site)
        else:
            search_limit = get_internal_search_limit(limit)
            search_depth = get_search_depth(site, search_limit)
            search_url = build_search_url(
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
                items = scrape_search_result(
                    search_url=search_url,
                    max_items=search_limit,
                    max_scroll=search_depth,
                    headless=True,
                )
                finalize(items, "mercari")
    except Exception as exc:
        logger.exception("Scrape task failed for site=%s", site)
        error_msg = str(exc)
        items = []
        new_count = 0
        updated_count = 0

    return {
        "items": items,
        "new_count": new_count,
        "updated_count": updated_count,
        "excluded_count": excluded_count,
        "error_msg": error_msg,
        "search_url": search_url,
        "keyword": keyword,
        "price_min": normalized_price_min,
        "price_max": normalized_price_max,
        "sort": sort,
        "category": category,
        "limit": limit,
        "site": site,
        "persist_to_db": persist_to_db,
        "shop_id": shop_id,
    }


def run_enqueued_scrape_job(scrape_job_id: str, request_payload: dict[str, Any]) -> dict[str, Any]:
    return run_tracked_job(scrape_job_id, execute_scrape_job, request_payload)
