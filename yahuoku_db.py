"""
Yahoo Auctions scraper - Product detail scraping for auctions.yahoo.co.jp
Uses __NEXT_DATA__ JSON when available, similar to Yahoo Shopping.
"""
import logging
import re
import json
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from mercari_db import create_driver

logger = logging.getLogger("yahuoku")

# CSS Selectors
SELECTORS = {
    "title": "h1",
    "price": ".Price__value",
    "countdown": ".CountDown__time",
    "seller": ".Seller__name a",
    "description": "#ProductDescription",
    "images": ".slick-slide img",
    "next_data": "#__NEXT_DATA__",
}


def extract_next_data(driver) -> dict:
    """Extract auction data from __NEXT_DATA__ JSON."""
    try:
        script = driver.find_element(By.CSS_SELECTOR, SELECTORS["next_data"])
        data = json.loads(script.get_attribute("innerHTML"))
        
        # Navigate to item data - path may vary
        props = data.get("props", {})
        page_props = props.get("pageProps", {})
        
        # Try different paths
        initial_state = page_props.get("initialState", {})
        item_detail = initial_state.get("item", {}).get("detail", {}).get("item", {})
        
        if item_detail:
            return item_detail
        
        # Alternative path
        initial_props = page_props.get("initialProps", {})
        auction_item = initial_props.get("auctionItem", {})
        
        return auction_item or {}
        
    except Exception as e:
        logger.debug(f"__NEXT_DATA__ extraction failed: {e}")
        return {}


def scrape_item_detail(driver, url: str) -> dict:
    """
    ヤフオクの商品ページから詳細情報を取得する
    """
    result = {
        "url": url,
        "title": "",
        "price": None,
        "status": "active",  # Auctions are active by default
        "description": "",
        "image_urls": [],
        "variants": [],
        "auction_id": "",
        "seller": "",
        "end_time": "",
    }
    
    try:
        driver.get(url)
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)  # Yahoo Auctions needs time to load
    except Exception as e:
        logger.error(f"Error accessing {url}: {e}")
        result["status"] = "error"
        return result
    
    # Extract auction ID from URL
    match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
    if match:
        result["auction_id"] = match.group(1)
    
    # Try __NEXT_DATA__ first
    next_data = extract_next_data(driver)
    
    if next_data:
        result["title"] = next_data.get("title", "")
        
        # Price structure varies
        price_data = next_data.get("price", {})
        if isinstance(price_data, dict):
            result["price"] = price_data.get("current") or price_data.get("bid")
        elif isinstance(price_data, (int, float)):
            result["price"] = int(price_data)
        
        seller_data = next_data.get("seller", {})
        if isinstance(seller_data, dict):
            result["seller"] = seller_data.get("name", "")
        
        result["auction_id"] = next_data.get("auctionID", result["auction_id"])
    
    # Fallback to CSS selectors
    if not result["title"]:
        try:
            title_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["title"])
            result["title"] = title_el.text.strip()
        except Exception:
            pass
    
    if result["price"] is None:
        try:
            price_els = driver.find_elements(By.CSS_SELECTOR, SELECTORS["price"])
            for el in price_els:
                text = el.text.strip()
                match = re.search(r"([\d,]+)", text)
                if match:
                    result["price"] = int(match.group(1).replace(",", ""))
                    break
        except Exception:
            pass
    
    # Time remaining
    try:
        countdown_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["countdown"])
        result["end_time"] = countdown_el.text.strip()
        
        # Check if ended
        if "終了" in result["end_time"]:
            result["status"] = "sold"
    except Exception:
        pass
    
    # Seller
    if not result["seller"]:
        try:
            seller_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["seller"])
            result["seller"] = seller_el.text.strip()
        except Exception:
            pass
    
    # Images
    try:
        img_els = driver.find_elements(By.CSS_SELECTOR, SELECTORS["images"])
        for img in img_els:
            src = img.get_attribute("src")
            if src and src not in result["image_urls"]:
                # Filter out placeholder images
                if "placeholder" not in src.lower():
                    result["image_urls"].append(src)
    except Exception:
        pass
    
    # Description
    try:
        desc_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["description"])
        result["description"] = desc_el.text.strip()
    except Exception:
        # Try finding description near "商品説明" heading
        try:
            headings = driver.find_elements(By.TAG_NAME, "h2")
            for h in headings:
                if "商品説明" in h.text:
                    sibling = h.find_element(By.XPATH, "following-sibling::*[1]")
                    result["description"] = sibling.text.strip()
                    break
        except Exception:
            pass
    
    # Default variant
    if result["price"]:
        result["variants"] = [{
            "option1_value": "Default Title",
            "price": result["price"],
            "sku": result["auction_id"],
            "inventory_qty": 1 if result["status"] == "active" else 0
        }]
    
    logger.info(f"Scraped: {result['title'][:30]}... - ¥{result['price']} ({result['status']})")
    return result


def scrape_single_item(url: str, headless: bool = True) -> list:
    """
    指定されたヤフオク商品URLを1件だけスクレイピング
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
    ヤフオク検索結果から複数商品をスクレイピング
    """
    driver = None
    results = []
    
    try:
        driver = create_driver(headless=headless)
        driver.get(search_url)
        time.sleep(2)
        
        # Find product links
        product_urls = set()
        product_selectors = [
            ".Product__titleLink",
            "a[href*='/auction/']",
        ]
        
        for selector in product_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    href = el.get_attribute("href")
                    if href and "/auction/" in href:
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
