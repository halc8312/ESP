"""
SNKRDUNK (snkrdunk.com) scraping module.
Based on rakuma_db.py architecture, adapted for SNKRDUNK's DOM structure.
"""
import logging
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


def scrape_single_item(url: str, headless: bool = True):
    """
    指定されたSNKRDUNK商品URLを1件だけスクレイピングして list[dict] を返す。
    save_scraped_items_to_db にそのまま渡せるようにリストに包んでいる。
    """
    driver = None
    metrics = get_metrics()
    metrics.start('snkrdunk', 'single')
    try:
        print(f"DEBUG: Starting SNKRDUNK scrape_single_item for {url}")
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
