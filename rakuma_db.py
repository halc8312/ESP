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
import re
import time
from selector_config import get_selectors, get_valid_domains
from scrape_metrics import get_metrics, log_scrape_result, check_scrape_health


def _parse_item_page(page, url: str) -> dict:
    """
    Scrapling の page オブジェクトから商品情報を抽出する共通ロジック。
    sync / async 両方の fetch 結果に対して使える。
    """
    # ---- デバッグ: ページ内容を確認 ----
    body_text = ""
    try:
        body_text = page.get_text() or ""
        logging.debug(f"Page text preview (first 500 chars): {body_text[:500]}")
    except Exception as e:
        logging.debug(f"Error getting page text: {e}")

    # <title> タグからタイトル候補を取得
    page_title = ""
    try:
        title_nodes = page.css("title")
        title_el = title_nodes[0] if title_nodes else None
        if title_el:
            page_title = (title_el.text or "").strip()
            logging.debug(f"<title> tag content: {page_title}")
    except Exception:
        pass

    # ---- デバッグ: 見出しタグ確認 ----
    try:
        for tag in ["h1", "h2", "h3", "h4"]:
            elements = page.css(tag)
            for el in elements:
                logging.debug(f"Found <{tag}>: {(el.text or '').strip()[:100]}")
    except Exception:
        pass

    # ---- タイトル ----
    title = ""
    title_selectors = get_selectors('rakuma', 'detail', 'title') or ["h1.item__name", "h1"]
    try:
        for selector in title_selectors:
            nodes = page.css(selector)
            el = nodes[0] if nodes else None
            if el:
                title = (el.text or "").strip()
                if title:
                    break
    except Exception as e:
        logging.debug(f"Error extracting title: {e}")

    # セレクタで取れない場合は <title> タグから抽出
    if not title and page_title:
        title = page_title
        for suffix in [" | ラクマ", "の商品写真", " - ラクマ", " - フリマアプリ ラクマ"]:
            if suffix in title:
                title = title.split(suffix)[0].strip()
        # "ブランド名)の実際のタイトル" パターン
        if ")の" in title:
            title = title.split(")の", 1)[-1].strip()
        logging.debug(f"Title from <title> tag: {title}")

    # ---- 価格 ----
    price = None
    price_selectors = get_selectors('rakuma', 'detail', 'price') or ["span.item__price", ".item__price"]
    try:
        for selector in price_selectors:
            nodes = page.css(selector)
            el = nodes[0] if nodes else None
            if el:
                price_text = el.text or ""
                m = re.search(r"[¥￥]\s*([\d,]+)", price_text) or re.search(r"([\d,]+)", price_text)
                if m:
                    price = int(m.group(1).replace(",", ""))
                    break
    except Exception as e:
        logging.debug(f"Error extracting price: {e}")

    # 予備の価格取得（body全体から）
    if price is None:
        try:
            m = re.search(r"[¥￥]\s*([\d,]+)", body_text)
            if not m:
                m = re.search(r"([\d,]+)\s*円", body_text)
            if m:
                price = int(m.group(1).replace(",", ""))
        except Exception:
            pass

    # ---- 説明文 ----
    description = ""
    desc_selectors = get_selectors('rakuma', 'detail', 'description') or ["div.item__description", ".item-description"]
    try:
        for selector in desc_selectors:
            nodes = page.css(selector)
            el = nodes[0] if nodes else None
            if el:
                description = (el.text or "").strip()
                if description:
                    break
    except Exception as e:
        logging.debug(f"Error extracting description: {e}")

    # セレクタで取れない場合は body テキストから抽出
    if not description and body_text:
        try:
            idx = body_text.find("商品説明")
            if idx >= 0:
                end_idx = body_text.find("商品情報", idx)
                if end_idx < 0:
                    end_idx = idx + 500
                description = body_text[idx + len("商品説明"):end_idx].strip()
        except Exception:
            pass

    # ---- 画像 ----
    image_urls = []
    image_selectors = get_selectors('rakuma', 'detail', 'images') or [".sp-image"]
    try:
        for selector in image_selectors:
            imgs = page.css(selector)
            for img in imgs:
                src = img.attrib.get("src", "")
                if not src or "placeholder" in src.lower() or "blank" in src.lower():
                    src = img.attrib.get("data-lazy") or img.attrib.get("data-src") or ""
                
                # Handling div background-images for sold-out items
                if not src:
                    style = img.attrib.get("style", "")
                    m = re.search(r'background-image:\s*url\(([^)]+)\)', style)
                    if m:
                        src = m.group(1).strip("'\"")

                if src and src not in image_urls and src.startswith("http"):
                    image_urls.append(src)
    except Exception as e:
        logging.debug(f"Error extracting images: {e}")

    # ---- ステータス（売り切れ判定） ----
    status = "on_sale"
    try:
        if "SOLDOUT" in body_text or "SOLD OUT" in body_text or "売り切れ" in body_text:
            status = "sold"

        sold_selectors = ["span.soldout", ".soldout-section", ".label-soldout", ".item-sell-out-badge"]
        for sel in sold_selectors:
            nodes = page.css(sel)
            sold_el = nodes[0] if nodes else None
            if sold_el:
                status = "sold"
                break
    except Exception as e:
        logging.debug(f"Error checking status: {e}")

    # ---- バリエーション（ラクマは基本的に単品販売） ----
    variants = []

    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": variants
    }


def scrape_item_detail(url: str, driver=None):
    """
    ラクマの商品ページから詳細情報を取得する（同期版）。

    Scrapling Fetcher（HTTP リクエストベース）を使用。
    item.fril.jp は SSR のためブラウザ不要。
    driver 引数は後方互換のために保持するが、使用しない。
    トップレベル（asyncioループ外）からの呼び出し用。
    """
    from scrapling import Fetcher

    try:
        page = Fetcher.get(
            url,
            stealthy_headers=True,
            follow_redirects=True,
        )
    except Exception as e:
        logging.error(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error",
            "description": "", "image_urls": [], "variants": []
        }

    return _parse_item_page(page, url)


async def _scrape_item_detail_async(url: str) -> dict:
    """
    ラクマの商品ページから詳細情報を取得する（async版）。

    Scrapling AsyncFetcher（HTTP リクエストベース）を使用。
    item.fril.jp は SSR のためブラウザ不要。
    _scrape_search_async 内（asyncioループ内）からの呼び出し用。
    """
    from scrapling.fetchers import AsyncFetcher

    try:
        page = await AsyncFetcher.get(url, stealthy_headers=True, follow_redirects=True)
    except Exception as e:
        logging.error(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error",
            "description": "", "image_urls": [], "variants": []
        }

    return _parse_item_page(page, url)


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

        if data["title"]:
            print(f"DEBUG: Success -> {data['title']}")
        else:
            print("DEBUG: Failed to get title")

        metrics.finish()
        return [data]

    except Exception as e:
        print(f"CRITICAL ERROR during Rakuma single scraping: {e}")
        import traceback
        traceback.print_exc()
        metrics.record_attempt(False, url, str(e))
        metrics.finish()
        return []


def _get_or_create_event_loop():
    """スレッドセーフなイベントループ取得"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


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

        try:
            await pw_page.goto(search_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logging.warning(f"Navigation timeout/error for {search_url}: {e}")

        print(f"DEBUG: Page Title = {await pw_page.title()}")

        # 商品リンクを収集
        hrefs = set()
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
                    hrefs.add(h)

            if len(hrefs) >= max_items * 1.5:
                break

            scroll_attempts += 1

        await browser.close()

    print(f"DEBUG: Found {len(hrefs)} unique links on search page.")

    # URLリストをフィルタ
    valid_domains = get_valid_domains('rakuma', 'search') or ["item.fril.jp", "fril.jp"]
    item_urls = [
        h for h in hrefs
        if any(domain in h for domain in valid_domains)
    ]

    # 各商品を AsyncFetcher (HTTP) でスクレイピング
    filtered_items = []
    for item_url in item_urls:
        if len(filtered_items) >= max_items:
            break

        print(f"DEBUG: Scraping Rakuma item {item_url}")
        try:
            data = await _scrape_item_detail_async(item_url)
            if data["title"] and data["status"] != "error":
                print(f"DEBUG: Success -> {data['title']}")
                filtered_items.append(data)
            else:
                print("DEBUG: Failed to get valid data (empty title or error)")
        except Exception as e:
            print(f"DEBUG: Error scraping {item_url}: {e}")

        await asyncio.sleep(1)

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
        loop = _get_or_create_event_loop()
        return loop.run_until_complete(
            _scrape_search_async(search_url, max_items, max_scroll)
        )
    except Exception as e:
        print(f"CRITICAL ERROR during Rakuma scraping: {e}")
        import traceback
        traceback.print_exc()
        return []
