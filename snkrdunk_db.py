"""
SNKRDUNK (snkrdunk.com) scraping module.
Based on rakuma_db.py architecture, adapted for SNKRDUNK's DOM structure.
"""
import logging
import json
import re
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from mercari_db import create_driver
from selector_config import get_selectors, get_valid_domains
from scrape_metrics import get_metrics, log_scrape_result, check_scrape_health


def scrape_item_detail(driver, url: str):
    """
    SNKRDUNKの商品ページから詳細情報を取得する
    """
    try:
        driver.get(url)
    except Exception as e:
        logging.error(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error",
            "description": "", "image_urls": [], "variants": []
        }

    wait = WebDriverWait(driver, 15)
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)  # SNKRDUNKはSPAなので十分な待機時間が必要
    except Exception:
        pass

    # ---- タイトル ----
    title = ""
    title_selectors = get_selectors('snkrdunk', 'detail', 'title') or [
        "h1.product-name-en",
        "h1[class*='product-name']",
        "h1"
    ]
    try:
        for selector in title_selectors:
            title_els = driver.find_elements(By.CSS_SELECTOR, selector)
            if title_els:
                title = title_els[0].text.strip()
                if title:
                    break
    except Exception as e:
        logging.debug(f"Error extracting title: {e}")

    # ---- 価格 ----
    price = None
    price_selectors = get_selectors('snkrdunk', 'detail', 'price') or [
        ".new-buy-button",
        "[class*='buy-button']"
    ]
    try:
        for selector in price_selectors:
            price_els = driver.find_elements(By.CSS_SELECTOR, selector)
            if price_els:
                price_text = price_els[0].text
                m = re.search(r"[¥￥]\s*([\d,]+)", price_text) or re.search(r"([\d,]+)", price_text)
                if m:
                    price = int(m.group(1).replace(",", ""))
                    break
    except Exception as e:
        logging.debug(f"Error extracting price: {e}")

    # 予備の価格取得（body全体から）
    if price is None:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            m = re.search(r"[¥￥]\s*([\d,]+)", body_text)
            if not m:
                m = re.search(r"([\d,]+)\s*円", body_text)
            if m:
                price = int(m.group(1).replace(",", ""))
        except Exception:
            pass

    # ---- 説明文 ----
    description = ""
    desc_selectors = get_selectors('snkrdunk', 'detail', 'description') or [
        ".item-article-text p",
        ".item-article-textbox",
        "[class*='article-text']"
    ]
    try:
        for selector in desc_selectors:
            desc_els = driver.find_elements(By.CSS_SELECTOR, selector)
            if desc_els:
                description = desc_els[0].text.strip()
                if description:
                    break
    except Exception as e:
        logging.debug(f"Error extracting description: {e}")

    # 商品情報テーブルからも情報取得を試みる
    if not description:
        try:
            info_els = driver.find_elements(By.CSS_SELECTOR, ".product-info-wrapper")
            if info_els:
                description = info_els[0].text.strip()
        except Exception:
            pass

    # ---- 画像 ----
    image_urls = []
    image_selectors = get_selectors('snkrdunk', 'detail', 'images') or [
        ".product-img img",
        "[class*='product-img'] img",
        "img[src*='snkrdunk']"
    ]
    try:
        for selector in image_selectors:
            imgs = driver.find_elements(By.CSS_SELECTOR, selector)
            for img in imgs:
                # 遅延読み込み対応: src, data-lazy, data-src をチェック
                src = img.get_attribute("src")
                if not src or "placeholder" in src.lower() or "blank" in src.lower():
                    src = img.get_attribute("data-lazy") or img.get_attribute("data-src")
                if src and src not in image_urls and src.startswith("http"):
                    image_urls.append(src)
            if image_urls:
                break
    except Exception as e:
        logging.debug(f"Error extracting images: {e}")

    # ---- ステータス（売り切れ判定） ----
    status = "on_sale"
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        # SNKRDUNKの売り切れ表示パターン
        if "SOLD OUT" in body_text or "売り切れ" in body_text or "在庫なし" in body_text:
            status = "sold"
    except Exception as e:
        logging.debug(f"Error checking status: {e}")

    # ---- バリエーション（SNKRDUNKはサイズ違いがあるが基本的に単品として扱う） ----
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


def scrape_item_detail_light(url: str) -> dict:
    """
    Light HTTP-only scrape for SNKRDUNK using Scrapling Fetcher.
    Extracts data from the embedded __NEXT_DATA__ JSON without launching a browser.
    Memory: ~5 MB (vs ~400 MB for Chrome). Returns empty dict on failure.
    """
    try:
        from services.scraping_client import fetch_static
        page = fetch_static(url)

        script_el = page.find("#__NEXT_DATA__")
        if not script_el:
            return {}

        json_str = str(script_el.text or "").strip()
        if not json_str:
            return {}

        data = json.loads(json_str)
        page_props = data.get("props", {}).get("pageProps", {})

        # Explore multiple common Next.js paths for product data
        item = (
            page_props.get("item")
            or page_props.get("product")
            or page_props.get("initialState", {}).get("item", {})
            or page_props.get("initialState", {}).get("product", {})
            or {}
        )

        if not item:
            return {}

        result = {
            "url": url,
            "title": "",
            "price": None,
            "status": "on_sale",
            "description": "",
            "image_urls": [],
            "variants": [],
        }

        # Title
        result["title"] = (
            item.get("name")
            or item.get("title")
            or item.get("productName")
            or item.get("nameEn")
            or ""
        )

        # Price
        price_raw = (
            item.get("minPrice")
            or item.get("price")
            or item.get("lowestPrice")
            or item.get("currentPrice")
        )
        if price_raw is not None:
            try:
                result["price"] = int(price_raw)
            except (ValueError, TypeError):
                m = re.search(r"([\d,]+)", str(price_raw))
                if m:
                    result["price"] = int(m.group(1).replace(",", ""))

        # Images
        images_raw = item.get("images") or item.get("imageList") or item.get("thumbnails") or []
        if isinstance(images_raw, list):
            for img in images_raw:
                if isinstance(img, dict):
                    img_url = img.get("url") or img.get("src") or img.get("imageUrl")
                elif isinstance(img, str):
                    img_url = img
                else:
                    img_url = None
                if img_url and img_url.startswith("http") and img_url not in result["image_urls"]:
                    result["image_urls"].append(img_url)
        elif isinstance(images_raw, dict):
            for key in ("list", "main", "items"):
                for img in images_raw.get(key, []):
                    img_url = img.get("url") or img.get("src") if isinstance(img, dict) else img
                    if img_url and img_url.startswith("http") and img_url not in result["image_urls"]:
                        result["image_urls"].append(img_url)
        # Fallback: og:image
        if not result["image_urls"]:
            og_img = page.css("meta[property='og:image']")
            if og_img:
                src = str(og_img[0].attrib.get("content", ""))
                if src and src.startswith("http"):
                    result["image_urls"].append(src)

        # Description
        result["description"] = (
            item.get("description")
            or item.get("itemDescription")
            or item.get("detail")
            or ""
        )
        if not result["description"]:
            meta_desc = page.css("meta[name='description']")
            if meta_desc:
                result["description"] = str(meta_desc[0].attrib.get("content", ""))

        # Status
        sold_out = (
            item.get("isSoldOut")
            or item.get("soldOut")
            or item.get("status", "") in ("sold_out", "sold", "inactive", "SOLD_OUT")
            or item.get("stock", 1) == 0
        )
        if sold_out:
            result["status"] = "sold"
        else:
            page_text = str(page.get_all_text())
            if "SOLD OUT" in page_text or "売り切れ" in page_text or "在庫なし" in page_text:
                result["status"] = "sold"

        if result["title"]:
            print(f"DEBUG [light]: SNKRDUNK light scrape success -> {result['title'][:40]}")
        return result

    except Exception as e:
        logging.debug(f"SNKRDUNK light scrape error: {e}")
        return {}


def scrape_single_item(url: str, headless: bool = True):
    """
    指定されたSNKRDUNK商品URLを1件だけスクレイピングして list[dict] を返す。
    Scrapling HTTP-onlyで試み、失敗時はSeleniumにフォールバック。
    save_scraped_items_to_db にそのまま渡せるようにリストに包んでいる。
    """
    metrics = get_metrics()
    metrics.start('snkrdunk', 'single')

    # Attempt 1: HTTP-only (fast, low memory)
    try:
        data = scrape_item_detail_light(url)
        if data and data.get("title"):
            log_scrape_result('snkrdunk', url, data)
            print(f"DEBUG: SNKRDUNK light scrape success -> {data['title']}")
            metrics.finish()
            return [data]
    except Exception as e:
        logging.debug(f"SNKRDUNK light scrape attempt failed: {e}")

    logging.debug("SNKRDUNK light scrape failed, falling back to Selenium")

    # Attempt 2: Selenium fallback
    driver = None
    try:
        print(f"DEBUG: Starting SNKRDUNK Selenium scrape for {url}")
        driver = create_driver(headless=headless)

        data = scrape_item_detail(driver, url)
        log_scrape_result('snkrdunk', url, data)

        if data["title"]:
            print(f"DEBUG: Success -> {data['title']}")
        else:
            print("DEBUG: Failed to get title")

        metrics.finish()
        return [data]

    except Exception as e:
        print(f"CRITICAL ERROR during SNKRDUNK single scraping: {e}")
        import traceback
        traceback.print_exc()
        metrics.record_attempt(False, url, str(e))
        metrics.finish()
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e:
                logging.debug("Error quitting driver: %s", e)


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
):
    """
    SNKRDUNK検索URLから複数商品をスクレイピングして list[dict] を返す。
    """
    driver = None
    try:
        print(f"DEBUG: Starting SNKRDUNK scrape_search_result")
        driver = create_driver(headless=headless)

        print(f"DEBUG: Navigating to {search_url}")
        driver.get(search_url)
        print(f"DEBUG: Page Title = {driver.title}")

        wait = WebDriverWait(driver, 15)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(3)  # SPAのロード待機
        except Exception:
            print("DEBUG: Timeout waiting for body")

        # 商品リンクを収集
        links = []
        scroll_attempts = 0
        link_selectors = get_selectors('snkrdunk', 'search', 'item_links') or [
            "a[class*='productTile']",
            "a[href*='/products/']"
        ]

        while len(links) < max_items * 2 and scroll_attempts < max_scroll * 2:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

            new_links = []
            for selector in link_selectors:
                new_links = driver.find_elements(By.CSS_SELECTOR, selector)
                if new_links:
                    break

            if not new_links:
                break

            # 重複除去
            current_hrefs = {l.get_attribute("href") for l in links if l.get_attribute("href")}
            for nl in new_links:
                h = nl.get_attribute("href")
                if h and h not in current_hrefs:
                    links.append(nl)

            if len(links) >= max_items * 1.5:
                break

            scroll_attempts += 1

        print(f"DEBUG: Found {len(links)} unique links on search page.")

        # URLリストを作成
        item_urls = []
        seen = set()
        valid_domains = get_valid_domains('snkrdunk', 'search') or ["snkrdunk.com"]

        for link in links:
            href = link.get_attribute("href")
            if not href or href in seen:
                continue
            # ドメインチェック
            is_valid = any(domain in href for domain in valid_domains)
            # 商品ページのみを対象（/products/を含むURL）
            if is_valid and "/products/" in href:
                seen.add(href)
                item_urls.append(href)

        # 各商品をスクレイピング
        filtered_items = []
        for url in item_urls:
            if len(filtered_items) >= max_items:
                break

            print(f"DEBUG: Scraping SNKRDUNK item {url}")
            try:
                data = scrape_item_detail(driver, url)
                if data["title"] and data["status"] != "error":
                    print(f"DEBUG: Success -> {data['title']}")
                    filtered_items.append(data)
                else:
                    print("DEBUG: Failed to get valid data (empty title or error)")
            except Exception as e:
                print(f"DEBUG: Error scraping {url}: {e}")

            time.sleep(1)

        return filtered_items

    except Exception as e:
        print(f"CRITICAL ERROR during SNKRDUNK scraping: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e:
                logging.debug("Error quitting driver: %s", e)
