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
    "title": "h1.ProductTitle__text, h1",
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
    
    # Description - Multiple approaches
    # Approach 1: Try #ProductDescription
    try:
        desc_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["description"])
        result["description"] = desc_el.text.strip()
    except Exception:
        pass
    
    # Approach 2: Find section containing "商品説明" and get sibling/child content
    if not result["description"]:
        try:
            # Find h2 with "商品説明"
            headings = driver.find_elements(By.TAG_NAME, "h2")
            for h in headings:
                if "商品説明" in h.text:
                    # Get parent section and find content div
                    section = h.find_element(By.XPATH, "./ancestor::section")
                    # Get all text within section excluding the header
                    all_divs = section.find_elements(By.TAG_NAME, "div")
                    for div in all_divs:
                        text = div.text.strip()
                        if text and "商品説明" not in text and len(text) > 50:
                            result["description"] = text
                            break
                    if result["description"]:
                        break
        except Exception:
            pass
    
    # Approach 3: Check for iframe with description
    if not result["description"]:
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                iframe_id = iframe.get_attribute("id") or ""
                iframe_name = iframe.get_attribute("name") or ""
                if "desc" in iframe_id.lower() or "desc" in iframe_name.lower():
                    driver.switch_to.frame(iframe)
                    body = driver.find_element(By.TAG_NAME, "body")
                    result["description"] = body.text.strip()
                    driver.switch_to.default_content()
                    break
        except Exception:
            driver.switch_to.default_content()
    
    # Approach 4: Search body text for description near relevant keywords
    if not result["description"]:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            # Find text after "商品説明" marker
            if "商品説明" in body_text:
                start_idx = body_text.find("商品説明") + len("商品説明")
                # Find end marker (next section like "発送について" or "支払い")
                end_markers = [
                    "発送について", "支払いについて", "注意事項", "送料", "配送方法",
                    "発送詳細", "支払い詳細", "お支払い", "落札者の方へ", 
                    "連絡掲示板", "出品者情報", "質問する", "ウォッチ",
                    "入札件数", "残り時間", "出品地域", "出品者", "評価"
                ]
                end_idx = len(body_text)
                for marker in end_markers:
                    marker_idx = body_text.find(marker, start_idx)
                    if marker_idx > 0 and marker_idx < end_idx:
                        end_idx = marker_idx
                desc_text = body_text[start_idx:end_idx].strip()
                
                # Clean up the description - remove common noise patterns
                lines = desc_text.split('\n')
                cleaned_lines = []
                for line in lines:
                    line = line.strip()
                    # Skip empty lines and very short lines
                    if not line or len(line) < 3:
                        continue
                    # Skip lines that look like metadata/table headers
                    skip_patterns = [
                        "全国一律", "送料無料", "匿名配送", "発送元", "発送日",
                        "着払い", "離島", "配送", "追跡番号", "補償", "定形外",
                        "ゆうパック", "ヤマト", "佐川", "クリックポスト", "ネコポス",
                        "発送までの日数", "入金確認後", "円～", "～円", "円　～"
                    ]
                    if any(pattern in line for pattern in skip_patterns):
                        continue
                    cleaned_lines.append(line)
                
                desc_text = '\n'.join(cleaned_lines).strip()
                if len(desc_text) > 20:
                    result["description"] = desc_text[:2000]  # Limit to 2000 chars
        except Exception:
            pass
    
    logger.debug(f"Description length: {len(result['description'])}")
    
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


def scrape_item_detail_light(url: str) -> dict:
    """
    Light HTTP-only scrape for Yahoo Auctions using Scrapling Fetcher.
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
        props = data.get("props", {})
        page_props = props.get("pageProps", {})

        # Try primary path
        initial_state = page_props.get("initialState", {})
        item_detail = initial_state.get("item", {}).get("detail", {}).get("item", {})

        # Try alternative path
        if not item_detail:
            initial_props = page_props.get("initialProps", {})
            item_detail = initial_props.get("auctionItem", {})

        if not item_detail:
            return {}

        result = {
            "url": url,
            "title": item_detail.get("title", ""),
            "price": None,
            "status": "active",
            "description": "",
            "image_urls": [],
            "variants": [],
            "auction_id": "",
            "seller": "",
            "end_time": "",
        }

        # Price
        price_data = item_detail.get("price", {})
        if isinstance(price_data, dict):
            result["price"] = price_data.get("current") or price_data.get("bid")
        elif isinstance(price_data, (int, float)):
            result["price"] = int(price_data)
        # Alternative path (auctionItem)
        if result["price"] is None:
            result["price"] = item_detail.get("currentPrice") or item_detail.get("price")

        # Description
        description = item_detail.get("description", "") or item_detail.get("itemDescription", "")
        if description:
            result["description"] = description
        else:
            # Fallback: meta[name='description']
            meta_el = page.css("meta[name='description']")
            if meta_el:
                result["description"] = str(meta_el[0].attrib.get("content", "") or "")

        # Images
        image_urls = []
        for key in ("images", "image", "imageList"):
            imgs = item_detail.get(key)
            if imgs is None:
                continue
            if isinstance(imgs, str) and imgs.startswith("http"):
                if imgs not in image_urls:
                    image_urls.append(imgs)
            elif isinstance(imgs, list):
                for img in imgs:
                    if isinstance(img, str) and img.startswith("http") and img not in image_urls:
                        image_urls.append(img)
                    elif isinstance(img, dict):
                        img_url = img.get("url") or img.get("src") or img.get("image") or img.get("imageUrl")
                        if img_url and img_url.startswith("http") and img_url not in image_urls:
                            image_urls.append(img_url)
            elif isinstance(imgs, dict):
                img_url = imgs.get("url") or imgs.get("src") or imgs.get("image") or imgs.get("imageUrl")
                if img_url and img_url.startswith("http") and img_url not in image_urls:
                    image_urls.append(img_url)
        # Fallback: og:image meta tag
        if not image_urls:
            og_el = page.css("meta[property='og:image']")
            if og_el:
                og_url = str(og_el[0].attrib.get("content", "") or "")
                if og_url.startswith("http"):
                    image_urls.append(og_url)
        result["image_urls"] = image_urls

        # Status: check JSON flags first, then page text
        status_flag = item_detail.get("status") or item_detail.get("isFinished") or item_detail.get("isClosed")
        if status_flag in (True, "closed", "finished", "ended"):
            result["status"] = "sold"
        else:
            page_text = str(page.get_all_text())
            if "終了" in page_text or "落札" in page_text:
                result["status"] = "sold"

        # Seller
        seller_data = item_detail.get("seller", {})
        if isinstance(seller_data, dict):
            result["seller"] = seller_data.get("name", "")

        # Auction ID
        match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
        if match:
            result["auction_id"] = match.group(1)
        result["auction_id"] = item_detail.get("auctionID", result["auction_id"])

        # Default variant
        if result["price"]:
            result["variants"] = [{
                "option1_value": "Default Title",
                "price": result["price"],
                "sku": result["auction_id"],
                "inventory_qty": 1 if result["status"] == "active" else 0
            }]

        if result["title"]:
            print(f"DEBUG [light]: Yahuoku light scrape success -> {result['title'][:40]}")
        return result

    except Exception as e:
        logger.debug(f"Yahuoku light scrape error: {e}")
        return {}


def scrape_single_item(url: str, headless: bool = True) -> list:
    """
    指定されたヤフオク商品URLを1件だけスクレイピング。
    Scrapling HTTP-onlyで試み、失敗時はSeleniumにフォールバック。
    """
    # Attempt 1: HTTP-only (fast, low memory)
    data = scrape_item_detail_light(url)
    if data and data.get("title"):
        return [data]

    logger.debug("Yahuoku light scrape failed, falling back to Selenium")

    # Attempt 2: Selenium fallback
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
