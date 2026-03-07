"""
Offmall (Hard Off) scraper - Product detail scraping for netmall.hardoff.co.jp
Uses JSON-LD data when available for reliable extraction.
"""
import logging
import re
import json
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from yahoo_db import create_driver

logger = logging.getLogger("offmall")

# CSS Selectors
SELECTORS = {
    "brand": ".product-detail-cate-name",
    "title": ".product-detail-name h1",
    "model": ".product-detail-num",
    "price": ".product-detail-price__main",
    "cart_button": ".cart-add-button",
    "main_image": ".product-detail-image-main img",
    "thumbnails": ".product-detail-image-sub__button img",
    "spec_label": ".product-detail-spec-list__label",
    "spec_value": ".product-detail-spec-list__value",
    "json_ld": "script[type='application/ld+json']",
}


def extract_json_ld(driver) -> dict:
    """Extract product data from JSON-LD if available."""
    try:
        scripts = driver.find_elements(By.CSS_SELECTOR, SELECTORS["json_ld"])
        for script in scripts:
            try:
                data = json.loads(script.get_attribute("innerHTML"))
                if isinstance(data, dict) and data.get("@type") == "Product":
                    return data
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return {}


def scrape_item_detail(driver, url: str) -> dict:
    """
    オフモールの商品ページから詳細情報を取得する
    """
    result = {
        "url": url,
        "title": "",
        "price": None,
        "status": "unknown",
        "description": "",
        "image_urls": [],
        "variants": [],
        "brand": "",
        "condition": "",
    }
    
    try:
        driver.get(url)
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(1.5)
        
        # Check for "product not found" page
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "対象の商品はございません" in body_text or "ページが見つかりません" in body_text:
            logger.info(f"Product not available (sold/removed): {url}")
            result["status"] = "sold"
            result["title"] = "Sold/Removed"
            return result
    except Exception as e:
        logger.error(f"Error accessing {url}: {e}")
        result["status"] = "error"
        return result
    
    # Try JSON-LD first (most reliable)
    json_ld = extract_json_ld(driver)
    
    if json_ld:
        # Extract from JSON-LD
        result["title"] = json_ld.get("name", "")
        result["brand"] = json_ld.get("brand", {}).get("name", "") if isinstance(json_ld.get("brand"), dict) else json_ld.get("brand", "")
        result["description"] = json_ld.get("description", "")
        
        # Price from offers
        offers = json_ld.get("offers", {})
        if isinstance(offers, dict):
            price_str = str(offers.get("price", ""))
            if price_str:
                result["price"] = int(float(price_str))
            
            # Availability
            availability = offers.get("availability", "")
            if "InStock" in availability:
                result["status"] = "active"
            elif "OutOfStock" in availability:
                result["status"] = "sold"
        
        # Images
        images = json_ld.get("image", [])
        if isinstance(images, str):
            result["image_urls"] = [images]
        elif isinstance(images, list):
            result["image_urls"] = [img for img in images if isinstance(img, str)]
    
    # Fallback to CSS selectors if JSON-LD incomplete
    if not result["title"]:
        try:
            title_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["title"])
            result["title"] = title_el.text.strip()
        except Exception:
            pass
    
    if not result["brand"]:
        try:
            brand_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["brand"])
            result["brand"] = brand_el.text.strip()
        except Exception:
            pass
    
    if result["price"] is None:
        try:
            price_el = driver.find_element(By.CSS_SELECTOR, SELECTORS["price"])
            price_text = price_el.text.strip()
            match = re.search(r"([\d,]+)", price_text)
            if match:
                result["price"] = int(match.group(1).replace(",", ""))
        except Exception:
            pass
    
    if result["status"] == "unknown":
        try:
            cart_btn = driver.find_elements(By.CSS_SELECTOR, SELECTORS["cart_button"])
            if cart_btn and len(cart_btn) > 0:
                # Check if button is disabled
                is_disabled = cart_btn[0].get_attribute("disabled")
                result["status"] = "sold" if is_disabled else "active"
            else:
                result["status"] = "sold"
        except Exception:
            pass
    
    if not result["image_urls"]:
        try:
            main_img = driver.find_element(By.CSS_SELECTOR, SELECTORS["main_image"])
            src = main_img.get_attribute("src")
            if src:
                result["image_urls"].append(src)
            
            # Thumbnails
            thumb_imgs = driver.find_elements(By.CSS_SELECTOR, SELECTORS["thumbnails"])
            for img in thumb_imgs:
                src = img.get_attribute("src")
                if src and src not in result["image_urls"]:
                    result["image_urls"].append(src)
        except Exception:
            pass
    
    # Specifications (condition, etc.)
    try:
        spec_labels = driver.find_elements(By.CSS_SELECTOR, SELECTORS["spec_label"])
        spec_values = driver.find_elements(By.CSS_SELECTOR, SELECTORS["spec_value"])
        
        specs = []
        for i, (label, value) in enumerate(zip(spec_labels, spec_values)):
            label_text = label.text.strip()
            value_text = value.text.strip()
            specs.append(f"{label_text}: {value_text}")
            
            if "状態" in label_text or "ランク" in label_text:
                result["condition"] = value_text
        
        if specs and not result["description"]:
            result["description"] = "\n".join(specs)
    except Exception:
        pass
    
    # Default variant
    if result["price"]:
        result["variants"] = [{
            "option1_value": result.get("condition") or "Default Title",
            "price": result["price"],
            "sku": "",
            "inventory_qty": 1 if result["status"] == "active" else 0
        }]
    
    logger.info(f"Scraped: {result['title'][:30]}... - ¥{result['price']} ({result['status']})")
    return result


def scrape_item_detail_light(url: str) -> dict:
    """
    Light HTTP-only scrape for Offmall using Scrapling Fetcher.
    Extracts data from embedded JSON-LD without launching a browser.
    Memory: ~5 MB (vs ~400 MB for Chrome). Returns empty dict on failure.
    """
    try:
        from services.scraping_client import fetch_static
        page = fetch_static(url)

        # Check for sold/removed page
        page_text = str(page.get_all_text())
        if "対象の商品はございません" in page_text or "ページが見つかりません" in page_text:
            return {
                "url": url, "title": "Sold/Removed", "price": None,
                "status": "sold", "description": "", "image_urls": [],
                "variants": [], "brand": "", "condition": ""
            }

        # Extract JSON-LD
        result = {
            "url": url, "title": "", "price": None, "status": "unknown",
            "description": "", "image_urls": [], "variants": [],
            "brand": "", "condition": ""
        }

        scripts = page.css("script[type='application/ld+json']")
        json_ld = {}
        for script_el in scripts:
            try:
                raw = str(script_el.text or "").strip()
                if not raw:
                    continue
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    json_ld = data
                    break
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            json_ld = item
                            break
                if json_ld:
                    break
            except (json.JSONDecodeError, Exception):
                continue

        if not json_ld:
            return {}

        result["title"] = json_ld.get("name", "")
        brand = json_ld.get("brand", {})
        result["brand"] = brand.get("name", "") if isinstance(brand, dict) else str(brand or "")
        result["description"] = json_ld.get("description", "")

        offers = json_ld.get("offers", {})
        if isinstance(offers, dict):
            price_str = str(offers.get("price", ""))
            if price_str:
                try:
                    result["price"] = int(float(price_str))
                except (ValueError, TypeError):
                    pass
            availability = offers.get("availability", "")
            if "InStock" in availability:
                result["status"] = "active"
            elif "OutOfStock" in availability:
                result["status"] = "sold"

        images = json_ld.get("image", [])
        if isinstance(images, str):
            result["image_urls"] = [images]
        elif isinstance(images, list):
            result["image_urls"] = [img for img in images if isinstance(img, str)]

        # Additional images: og:image meta tag
        og_el = page.css("meta[property='og:image']")
        if og_el:
            og_url = str(og_el[0].attrib.get("content", "") or "")
            if og_url.startswith("http") and og_url not in result["image_urls"]:
                result["image_urls"].insert(0, og_url)

        # Additional images: product image elements in page
        for img_el in page.css("img[src*='hardoff']"):
            src = str(img_el.attrib.get("src", "") or "")
            if src.startswith("http") and src not in result["image_urls"]:
                result["image_urls"].append(src)

        # Condition (item rank) from JSON-LD itemCondition or page elements
        condition = json_ld.get("itemCondition", "")
        if condition:
            result["condition"] = re.sub(r"https?://schema\.org/", "", condition)
        else:
            # Try to find condition rank in page text (e.g. "Aランク", "Bランク")
            cond_els = page.css(".item-condition, .condition, [class*='rank'], [class*='condition']")
            if cond_els:
                result["condition"] = str(cond_els[0].text or "").strip()

        if result["price"]:
            result["variants"] = [{
                "option1_value": result.get("condition") or "Default Title",
                "price": result["price"],
                "sku": "",
                "inventory_qty": 1 if result["status"] == "active" else 0
            }]

        if result["title"]:
            print(f"DEBUG [light]: Offmall light scrape success -> {result['title'][:40]}")
        return result

    except Exception as e:
        logger.debug(f"Offmall light scrape error: {e}")
        return {}


def scrape_single_item(url: str, headless: bool = True) -> list:
    """
    指定されたオフモール商品URLを1件だけスクレイピング。
    Scrapling HTTP-onlyで試み、失敗時はSeleniumにフォールバック。
    """
    # Attempt 1: HTTP-only (fast, low memory)
    data = scrape_item_detail_light(url)
    if data and data.get("title"):
        return [data]

    logger.debug("Offmall light scrape failed, falling back to Selenium")

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
    オフモール検索結果から複数商品をスクレイピング
    """
    driver = None
    results = []
    
    try:
        driver = create_driver(headless=headless)
        driver.get(search_url)
        time.sleep(2)
        
        # Find product links
        product_urls = set()
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "a[href*='/product/']")
            for el in elements:
                href = el.get_attribute("href")
                if href and "/product/" in href and href not in product_urls:
                    product_urls.add(href)
                    if len(product_urls) >= max_items:
                        break
        except Exception:
            pass
        
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
