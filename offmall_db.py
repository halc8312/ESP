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
from mercari_db import create_driver

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


def scrape_single_item(url: str, headless: bool = True) -> list:
    """
    指定されたオフモール商品URLを1件だけスクレイピング
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
