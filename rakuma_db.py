"""
Rakuma (fril.jp) scraping module.
Based on mercari_db.py architecture, adapted for Rakuma's DOM structure.

item.fril.jp は SSR（サーバーサイドレンダリング）で提供されているため、
ブラウザ不要の HTTP フェッチャーを使用する。

- scrape_item_detail: Fetcher.get() (HTTP, sync)
- _scrape_item_detail_async: AsyncFetcher.get() (HTTP, async)
- scrape_search_result: Playwright async API (scroll support, 変更なし)
"""
import asyncio
import logging
from urllib.parse import urljoin

from selector_config import get_selectors
from scrape_metrics import get_metrics, log_scrape_result, check_scrape_health
from services.rakuma_item_parser import parse_rakuma_item_page
from services.scrape_safety import (
    ScrapeFailure,
    ScrapeHttpError,
    UnsafeScrapeUrlError,
    install_navigation_guard,
    is_usable_detail_result,
    raise_for_blocked_navigation,
    raise_for_unsafe_detail_result,
    require_search_outcome,
    require_usable_details,
    validate_fetch_response,
    validate_marketplace_url,
)
from services.scraping_client import (
    fetch_marketplace_static,
    fetch_marketplace_static_async,
    gather_with_concurrency,
    get_async_fetch_settings,
    run_coro_sync,
)


def scrape_item_detail(url: str, driver=None):
    """
    ラクマの商品ページから詳細情報を取得する（同期版）。

    Scrapling Fetcher（HTTP リクエストベース）を使用。
    item.fril.jp は SSR のためブラウザ不要。
    driver 引数は後方互換のために保持するが、使用しない。
    トップレベル（asyncioループ外）からの呼び出し用。
    """
    try:
        url = validate_marketplace_url(url, "rakuma", kind="detail")
        page = fetch_marketplace_static(
            url,
            site="rakuma",
            kind="detail",
        )
        validate_fetch_response(page, "rakuma", kind="detail")
    except ScrapeFailure:
        raise
    except Exception as e:
        logging.error(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error",
            "description": "", "image_urls": [], "variants": []
        }

    return parse_rakuma_item_page(page, url)


async def _scrape_item_detail_async(url: str) -> dict:
    """
    ラクマの商品ページから詳細情報を取得する（async版）。

    Scrapling AsyncFetcher（HTTP リクエストベース）を使用。
    item.fril.jp は SSR のためブラウザ不要。
    _scrape_search_async 内（asyncioループ内）からの呼び出し用。
    """
    settings = get_async_fetch_settings("rakuma")

    try:
        url = validate_marketplace_url(url, "rakuma", kind="detail")
        page = await fetch_marketplace_static_async(
            url,
            site="rakuma",
            kind="detail",
            timeout=settings.timeout,
            retries=settings.retries,
            backoff_seconds=settings.backoff_seconds,
        )
        validate_fetch_response(page, "rakuma", kind="detail")
    except ScrapeFailure:
        raise
    except Exception as e:
        logging.error(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error",
            "description": "", "image_urls": [], "variants": []
        }

    return parse_rakuma_item_page(page, url)


def scrape_single_item(url: str, headless: bool = True):
    """
    指定されたラクマ商品URLを1件だけスクレイピングして list[dict] を返す。
    save_scraped_items_to_db にそのまま渡せるようにリストに包んでいる。
    """
    metrics = get_metrics()
    metrics.start('rakuma', 'single')
    try:
        print(f"DEBUG: Starting Rakuma scrape_single_item for {url}")

        data = scrape_item_detail(url)
        log_scrape_result('rakuma', url, data)

        if is_usable_detail_result(data):
            print(f"DEBUG: Success -> {data['title']}")
        else:
            print("DEBUG: Failed to get title")

        metrics.finish()
        return [data]

    except Exception as e:
        logging.exception("Critical error during Rakuma single scraping: %s", e)
        metrics.record_attempt(False, url, str(e))
        metrics.finish()
        raise


async def _scrape_search_async(search_url: str, max_items: int, max_scroll: int):
    """Playwright async API を使用してラクマ検索結果をスクレイピングする。"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        pw_page = await context.new_page()
        blocked_urls = await install_navigation_guard(context, "rakuma", kind="search")

        try:
            response = await pw_page.goto(search_url, wait_until="networkidle", timeout=30000)
            raise_for_blocked_navigation(blocked_urls, "rakuma")
            validate_fetch_response(response, "rakuma", kind="search")
            final_url = getattr(pw_page, "url", None)
            if isinstance(final_url, str) and final_url:
                validate_marketplace_url(final_url, "rakuma", kind="search")
        except ScrapeFailure:
            await browser.close()
            raise
        except Exception as e:
            await browser.close()
            raise_for_blocked_navigation(blocked_urls, "rakuma")
            raise ScrapeHttpError(f"ラクマの検索ページを取得できませんでした: {e}") from e

        print(f"DEBUG: Page Title = {await pw_page.title()}")

        # 商品リンクを収集
        hrefs = []
        seen_hrefs = set()
        scroll_attempts = 0
        link_selectors = get_selectors('rakuma', 'search', 'item_links') or [
            "a.link_search_image",
            "a.link_search_title"
        ]

        while len(hrefs) < max_items * 2 and scroll_attempts < max_scroll * 2:
            await pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await pw_page.wait_for_timeout(2000)

            new_links = []
            for selector in link_selectors:
                new_links = await pw_page.query_selector_all(selector)
                if new_links:
                    break

            if not new_links:
                break

            for nl in new_links:
                h = await nl.get_attribute("href")
                if h:
                    try:
                        h = validate_marketplace_url(
                            urljoin(search_url, h),
                            "rakuma",
                            kind="detail",
                        )
                    except UnsafeScrapeUrlError:
                        logging.warning("Ignored unsafe Rakuma detail URL discovered in search HTML")
                        continue
                    if h not in seen_hrefs:
                        seen_hrefs.add(h)
                        hrefs.append(h)

            if len(hrefs) >= max_items * 1.5:
                break

            scroll_attempts += 1

        try:
            body_text = await pw_page.locator("body").inner_text(timeout=3000)
        except Exception:
            body_text = ""
        await browser.close()
        raise_for_blocked_navigation(blocked_urls, "rakuma")

    print(f"DEBUG: Found {len(hrefs)} unique links on search page.")

    item_urls = list(hrefs)
    require_search_outcome(
        "rakuma",
        candidate_count=len(item_urls),
        text=body_text,
    )

    # 各商品を AsyncFetcher (HTTP) でスクレイピング
    filtered_items = []
    candidate_urls = item_urls[: max_items * 2]
    settings = get_async_fetch_settings("rakuma")
    detail_results = await gather_with_concurrency(
        candidate_urls,
        _scrape_item_detail_async,
        concurrency=settings.concurrency,
    )

    for data in detail_results:
        raise_for_unsafe_detail_result("rakuma", data)

    for item_url, data in zip(candidate_urls, detail_results):
        if len(filtered_items) >= max_items:
            break

        print(f"DEBUG: Scraping Rakuma item {item_url}")
        if isinstance(data, Exception):
            print(f"DEBUG: Error scraping {item_url}: {data}")
            continue
        if is_usable_detail_result(data):
            print(f"DEBUG: Success -> {data['title']}")
            filtered_items.append(data)
        else:
            print("DEBUG: Failed to get valid data (empty title or error)")

    require_usable_details(
        "rakuma",
        candidate_count=len(candidate_urls),
        item_count=len(filtered_items),
    )
    return filtered_items


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
):
    """
    ラクマ検索URLから複数商品をスクレイピングして list[dict] を返す。

    Playwright async API を使用してスクロール付き検索を実行し、
    各商品詳細は AsyncFetcher (HTTP) で取得する。
    """
    try:
        print(f"DEBUG: Starting Rakuma scrape_search_result")
        search_url = validate_marketplace_url(search_url, "rakuma", kind="search")
        return run_coro_sync(
            _scrape_search_async(search_url, max_items, max_scroll)
        )
    except Exception as e:
        logging.exception("Critical error during Rakuma search scraping: %s", e)
        raise
