"""
Surugaya scraper - Product detail scraping for suruga-ya.jp
"""
import logging
import re
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from mercari_db import create_driver

logger = logging.getLogger("surugaya")

# CSS Selectors - Updated based on browser investigation 2026-01-07
SELECTORS = {
    "title": "h1",
    "price": ".price_group .text-price-detail, .price_group label",
    "stock_available": ".btn_buy, .cart1",  # Both selectors work
    "stock_sold": ".waitbtn",
    "main_image": ".is-main-image img",
    "description": ".tbl_product_info",  # Fixed: was #product_detail (doesn't exist)
    "condition": ".price_group label",
    "category": ".left div a[href*='category=']",
}


def scrape_item_detail(driver, url: str) -> dict:
    """
    駿河屋の商品ページから詳細情報を取得する
    """
    result = {
        "url": url,
        "title": "",
        "price": None,
        "status": "unknown",
        "description": "",
        "image_urls": [],
        "variants": [],
        "condition": "",
        "category": "",
    }
    
    # Use print for Render logs (logger may not be configured)
    print(f"[SURUGAYA] Starting scrape for {url}")
    
    try:
        driver.get(url)
        print("[SURUGAYA] Page load initiated")
        
        wait = WebDriverWait(driver, 20)
        
        # First wait for body to exist
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        print("[SURUGAYA] Body element found")
        
        # Wait for JS to execute
        time.sleep(3)
        
        # Check for Cloudflare block
        page_source = driver.page_source
        if "cloudflare" in page_source.lower() or "challenge" in page_source.lower():
            print("[SURUGAYA] ERROR: Cloudflare/Challenge page detected!")
            result["status"] = "blocked"
            return result
        
        # Check page title to see what we got
        print(f"[SURUGAYA] Page title: {driver.title}")
        print(f"[SURUGAYA] Current URL: {driver.current_url}")
        
        # Check for H1
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h1")))
            print("[SURUGAYA] H1 element found")
        except Exception as h1_err:
            print(f"[SURUGAYA] WARNING: H1 not found: {h1_err}")
            
        time.sleep(1)
        
    except Exception as e:
        print(f"[SURUGAYA] ERROR during page load: {e}")
        result["status"] = "error"
        return result
    
    # ---- タイトル ----
    try:
        title_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["title"])
        result["title"] = title_el.text.strip()
        logger.debug(f"Title found: {result['title'][:50]}")
    except Exception as e:
        logger.warning(f"Title not found: {e}")
    
    # ---- 価格 ----
    # Try multiple price selectors
    try:
        price_els = driver.find_elements(By.CSS_SELECTOR, SELECTORS["price"])
        for el in price_els:
            text = el.text
            # Extract price from "中古 3,700円 (税込)" format
            match = re.search(r"([\d,]+)\s*円", text)
            if match:
                result["price"] = int(match.group(1).replace(",", ""))
                logger.debug(f"Price found: {result['price']}")
                break
    except Exception as e:
        logger.warning(f"Price extraction failed: {e}")
    
    # Fallback: search body text
    if result["price"] is None:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            match = re.search(r"([\d,]+)\s*円\s*\(税込\)", body_text)
            if match:
                result["price"] = int(match.group(1).replace(",", ""))
        except Exception:
            pass
    
    # ---- 在庫状態 ----
    try:
        buy_btn = driver.find_elements(By.CSS_SELECTOR, SELECTORS["stock_available"])
        sold_btn = driver.find_elements(By.CSS_SELECTOR, SELECTORS["stock_sold"])
        
        if buy_btn and len(buy_btn) > 0:
            result["status"] = "active"
        elif sold_btn and len(sold_btn) > 0:
            result["status"] = "sold"
        else:
            # Check for "品切れ" text
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if "品切れ" in body_text:
                result["status"] = "sold"
            else:
                result["status"] = "active"
    except Exception:
        result["status"] = "unknown"
    
    # ---- 商品状態（中古/新品）----
    try:
        condition_els = driver.find_elements(By.CSS_SELECTOR, SELECTORS["condition"])
        for el in condition_els:
            text = el.text.strip()
            if "中古" in text:
                result["condition"] = "中古"
                break
            elif "新品" in text:
                result["condition"] = "新品"
                break
    except Exception:
        pass
    
    # ---- 画像 ----
    try:
        img_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["main_image"])
        src = img_el.get_attribute("src")
        if src:
            result["image_urls"].append(src)
    except Exception:
        pass
    
    # ---- 説明（#product_detail テーブル）----
    try:
        detail_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["description"])
        result["description"] = detail_el.text.strip()
    except Exception:
        pass
    
    # ---- カテゴリ ----
    try:
        category_els = driver.find_elements(By.CSS_SELECTOR, SELECTORS["category"])
        if category_els:
            categories = [el.text.strip() for el in category_els if el.text.strip()]
            result["category"] = " > ".join(categories)
    except Exception:
        pass
    
    # Default variant for compatibility
    if result["price"]:
        result["variants"] = [{
            "option1_value": result.get("condition") or "Default Title",
            "price": result["price"],
            "sku": "",
            "inventory_qty": 1 if result["status"] == "active" else 0
        }]
    
    logger.info(f"Scraped: {result['title'][:30]}... - ¥{result['price']} ({result['status']})")
    return result


def scrape_single_item(url: str, headless: bool = True) -> list:
    """
    指定された駿河屋商品URLを1件だけスクレイピングして list[dict] を返す。
    """
    driver = None
    try:
        driver = create_driver(headless=headless)
        result = scrape_item_detail(driver, url)
        return [result] if result["title"] else []
    except Exception as e:
        logger.error(f"Error in scrape_single_item: {e}")
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
) -> list:
    """
    駿河屋検索結果から複数商品をスクレイピング
    """
    driver = None
    results = []
    
    try:
        driver = create_driver(headless=headless)
        driver.get(search_url)
        time.sleep(2)
        
        # Find product links
        # Surugaya uses .item class or similar for product cards
        product_selectors = [
            ".item a[href*='/product/detail/']",
            "a[href*='/product/detail/']",
        ]
        
        product_urls = set()
        for selector in product_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    href = el.get_attribute("href")
                    if href and "/product/detail/" in href:
                        product_urls.add(href)
                        if len(product_urls) >= max_items:
                            break
            except Exception:
                continue
            if len(product_urls) >= max_items:
                break
        
        # Scrape each product
        for url in list(product_urls)[:max_items]:
            try:
                result = scrape_item_detail(driver, url)
                if result["title"]:
                    results.append(result)
            except Exception as e:
                logger.error(f"Error scraping {url}: {e}")
                continue
        
        return results
        
    except Exception as e:
        logger.error(f"Error in scrape_search_result: {e}")
        return results
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
