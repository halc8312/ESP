"""
Worker-callable scrape task execution.
"""
from __future__ import annotations

import logging
from typing import Any

import offmall_db
import recordcity_db
import rakuma_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db
from mercari_db import scrape_search_result, scrape_single_item
from services.filter_service import filter_excluded_items, filter_items_by_price, normalize_price_bounds
from services.product_service import save_scraped_items_to_db
from services.scrape_job_runtime import (
    ScrapeJobAlreadyTerminated,
    assert_current_job_active,
    checkpoint_current_job,
    defer_current_job_observation,
    run_tracked_job,
)
from services.scrape_observation import (
    classify_scrape_failure,
    inspect_scraped_items,
    record_observation_safely,
)
from services.scrape_request import (
    build_search_url,
    classify_target_url,
    get_internal_search_limit,
    get_search_depth,
    normalize_scrape_limit,
    recordcity_listing_enabled,
)
from services.search_result_quality import (
    build_search_quality,
    deduplicate_search_items,
    inspect_search_quality,
)
from services.scrape_safety import SearchResult


logger = logging.getLogger("scrape_tasks")


def _record_task_observation(**observation):
    # Tracked workers publish only after winning the terminal-state transition;
    # standalone diagnostic calls retain their existing observation behavior.
    if not defer_current_job_observation(observation):
        record_observation_safely(**observation)


def _validate_scraper_result(scraped_items, *, site: str, allow_empty: bool = True):
    """
    Preserve a legitimate empty result while failing closed on scraper errors.

    Scrapers use an empty list for a valid search with no matches. Explicit
    blocked/error item states and malformed return values are operational
    failures and must reach run_tracked_job/RQ as exceptions.
    """
    if scraped_items is None:
        raise RuntimeError(f"{site}の商品抽出結果を取得できませんでした。")
    if not isinstance(scraped_items, (list, tuple)):
        raise RuntimeError(f"{site}の商品抽出結果の形式が不正です。")
    if not scraped_items and not allow_empty:
        raise RuntimeError(
            f"{site}の商品ページから商品情報を取得できませんでした。"
            "サイト側の表示変更または通信障害の可能性があります。"
        )

    failed_statuses = {
        str(item.get("status") or "").strip().lower()
        for item in scraped_items
        if isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() in {"blocked", "challenge", "error"}
    }
    if failed_statuses.intersection({"blocked", "challenge"}):
        raise RuntimeError(f"{site}へのアクセスが制限されました。時間をおいて再度お試しください。")
    if "error" in failed_statuses:
        raise RuntimeError(f"{site}の商品情報を取得できませんでした。時間をおいて再度お試しください。")
    if any(not isinstance(item, dict) for item in scraped_items):
        raise RuntimeError(f"{site}の商品抽出結果の形式が不正です。")
    if scraped_items and not any(str(item.get("title") or "").strip() for item in scraped_items):
        raise RuntimeError(
            f"{site}の商品ページは取得できましたが、商品情報を確認できませんでした。"
            "サイト側の表示変更の可能性があります。"
        )

    return list(scraped_items)


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
    target_kind = "search"
    if target_url:
        target_kind, site = classify_target_url(target_url)
    acquisition_mode = (
        "listing" if request_payload.get("acquisition_mode") == "listing"
        and site == "recordcity" and target_kind == "search" and recordcity_listing_enabled()
        else "detail"
    )
    limit = normalize_scrape_limit(
        request_payload.get("limit"), site=site, target_kind=target_kind,
        acquisition_mode=acquisition_mode,
    )
    user_id = request_payload.get("user_id")
    persist_to_db = bool(request_payload.get("persist_to_db", True))
    shop_id = request_payload.get("shop_id")

    items = []
    new_count = 0
    updated_count = 0
    excluded_count = 0
    search_url = ""
    normalized_price_min, normalized_price_max = normalize_price_bounds(price_min, price_max)
    observed_site = site
    observed_route = "search"
    observation = None
    failure_stage = "fetch_error"
    smoke_result = _get_smoke_result_payload(request_payload)
    scrape_started = False
    search_progress = {}
    search_quality = None

    def checkpoint(scraped_items, progress):
        # Only the RecordCity search adapter currently reports incremental work.
        # Keep validated snapshots durable without registering products twice.
        nonlocal search_progress
        search_progress = dict(progress)
        validated = _validate_scraper_result(scraped_items, site="recordcity")
        unique, duplicates = deduplicate_search_items(validated, site="recordcity")
        filtered, excluded = filter_excluded_items(unique, user_id)
        filtered, price_excluded = filter_items_by_price(
            filtered, price_min=normalized_price_min, price_max=normalized_price_max,
        )
        staged_items = filtered[:limit]
        checkpoint_current_job(
            {
                "items": staged_items, "new_count": 0, "updated_count": 0,
                "excluded_count": excluded + price_excluded, "error_msg": "",
                "search_url": search_url, "keyword": keyword,
                "price_min": normalized_price_min, "price_max": normalized_price_max,
                "sort": sort, "category": category, "limit": limit,
                "site": "recordcity", "persist_to_db": persist_to_db, "shop_id": shop_id,
                "acquisition_mode": acquisition_mode,
                "search_quality": build_search_quality(
                    unique, requested_count=limit, duplicate_count=duplicates, site="recordcity",
                    progress=progress, excluded_count=excluded + price_excluded,
                    displayed_count=len(staged_items),
                    acquisition_mode=acquisition_mode,
                ),
            },
            {**progress, "items_count": len(staged_items), "requested_count": limit},
        )
        if acquisition_mode == "detail" and len(staged_items) >= limit:
            # Only RecordCity's legacy detail loop consumes this explicit
            # signal. Count after exclusions/price filtering, and require every
            # returned row to have verified identity/price/status before ending
            # overfetch. Use the existing search-quality contract for that set.
            return build_search_quality(
                staged_items, requested_count=limit, site="recordcity",
            )["completion_verified"]
        return False

    def finalize(scraped_items, target_site, *, allow_empty=True):
        nonlocal items, excluded_count, new_count, updated_count
        nonlocal observation, failure_stage, search_quality
        assert_current_job_active()
        failure_stage = "invalid_result"
        # Validation intentionally copies list-compatible adapters to a plain
        # list. Preserve verified completion evidence before that copy.
        result_progress = dict(search_progress)
        if isinstance(scraped_items, SearchResult) and scraped_items.end_reason == "explicit_empty":
            result_progress["end_reason"] = "explicit_empty"
        scraped_items = _validate_scraper_result(
            scraped_items,
            site=target_site,
            allow_empty=allow_empty,
        )
        duplicates = 0
        if observed_route == "search" and smoke_result is None:
            scraped_items, duplicates = deduplicate_search_items(scraped_items, site=target_site)
            search_quality = build_search_quality(
                scraped_items, requested_count=limit, duplicate_count=duplicates, site=target_site,
                progress=result_progress,
                acquisition_mode=acquisition_mode,
            )
            observation = inspect_search_quality(scraped_items, search_quality)
        else:
            observation = inspect_scraped_items(scraped_items)
        failure_stage = "persistence_error"
        filtered_items, excluded_count = filter_excluded_items(scraped_items, user_id)
        filtered_items, price_excluded_count = filter_items_by_price(
            filtered_items,
            price_min=normalized_price_min,
            price_max=normalized_price_max,
        )
        excluded_count += price_excluded_count
        items = filtered_items[:limit]
        if search_quality is not None:
            search_quality["excluded_count"] = excluded_count
            search_quality["displayed_count"] = len(items)
        if persist_to_db:
            new_count, updated_count = save_scraped_items_to_db(
                items,
                site=target_site,
                user_id=user_id,
                shop_id=shop_id,
                raise_on_error=True,
            )

    try:
        if smoke_result is not None:
            smoke_error = str(smoke_result.get("error_msg") or "").strip()
            if smoke_error:
                raise RuntimeError(smoke_error)
            finalize(list(smoke_result.get("items") or []), str(smoke_result.get("site") or site))
            search_url = str(smoke_result.get("search_url") or f"internal://stack-smoke/{site}")
            keyword = str(smoke_result.get("keyword") or keyword)
            sort = str(smoke_result.get("sort") or sort)
            category = smoke_result.get("category", category)
            return {
                "items": items,
                "new_count": new_count,
                "updated_count": updated_count,
                "excluded_count": excluded_count,
                "error_msg": "",
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
                "acquisition_mode": acquisition_mode,
            }

        if target_url:
            url_kind, target_site = classify_target_url(target_url)
            observed_site = target_site
            observed_route = "search" if url_kind == "search" else "detail"
            if url_kind == "search":
                search_scraper_map = {
                    "yahoo": yahoo_db.scrape_search_result,
                    "rakuma": rakuma_db.scrape_search_result,
                    "surugaya": surugaya_db.scrape_search_result,
                    "offmall": offmall_db.scrape_search_result,
                    "yahuoku": yahuoku_db.scrape_search_result,
                    "snkrdunk": snkrdunk_db.scrape_search_result,
                    "recordcity": recordcity_db.scrape_search_result,
                    "mercari": scrape_search_result,
                }
                search_fn = search_scraper_map[target_site]
                search_url = target_url
                search_limit = get_internal_search_limit(limit)
                search_depth = get_search_depth(target_site, search_limit)
                scrape_started = True
                if acquisition_mode == "listing":
                    scraped = recordcity_db.scrape_listing_result(
                        search_url=target_url, max_items=limit,
                        max_pages=get_search_depth("recordcity", limit),
                        progress_callback=checkpoint,
                    )
                else:
                    scraped = search_fn(
                        search_url=target_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                        **({"progress_callback": checkpoint} if target_site == "recordcity" else {}),
                    )
                finalize(scraped, target_site)
            else:
                scraper_map = {
                    "yahoo": yahoo_db.scrape_single_item,
                    "rakuma": rakuma_db.scrape_single_item,
                    "surugaya": surugaya_db.scrape_single_item,
                    "offmall": offmall_db.scrape_single_item,
                    "yahuoku": yahuoku_db.scrape_single_item,
                    "snkrdunk": snkrdunk_db.scrape_single_item,
                    "recordcity": recordcity_db.scrape_single_item,
                    "mercari": scrape_single_item,
                }
                scraper_fn = scraper_map[target_site]
                scrape_started = True
                finalize(
                    scraper_fn(target_url, headless=True),
                    target_site,
                    allow_empty=False,
                )
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
            scrape_started = True

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
            elif site == "recordcity":
                if acquisition_mode == "listing":
                    items = recordcity_db.scrape_listing_result(
                        search_url=search_url, max_items=limit,
                        max_pages=get_search_depth("recordcity", limit),
                        progress_callback=checkpoint,
                    )
                else:
                    items = recordcity_db.scrape_search_result(
                        search_url=search_url,
                        max_items=search_limit,
                        max_scroll=search_depth,
                        headless=True,
                        progress_callback=checkpoint,
                    )
                finalize(items, "recordcity")
            else:
                observed_site = "mercari"
                items = scrape_search_result(
                    search_url=search_url,
                    max_items=search_limit,
                    max_scroll=search_depth,
                    headless=True,
                )
                finalize(items, "mercari")
    except Exception as exc:
        if smoke_result is None and scrape_started and not isinstance(exc, ScrapeJobAlreadyTerminated):
            _record_task_observation(
                site=observed_site,
                route=observed_route,
                outcome="failure",
                reason=classify_scrape_failure(exc, default=failure_stage),
                success_count=0,
                error_count=1,
            )
        logger.exception("Scrape task failed for site=%s", site)
        raise

    if smoke_result is None and observation is not None:
        _record_task_observation(site=observed_site, route=observed_route, **observation)

    return {
        "items": items,
        "new_count": new_count,
        "updated_count": updated_count,
        "excluded_count": excluded_count,
        "error_msg": "",
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
        "acquisition_mode": acquisition_mode,
        **({"search_quality": search_quality} if search_quality is not None else {}),
    }


def run_enqueued_scrape_job(scrape_job_id: str, request_payload: dict[str, Any]) -> dict[str, Any]:
    return run_tracked_job(scrape_job_id, execute_scrape_job, request_payload)
