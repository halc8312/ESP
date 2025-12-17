import logging
import re
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from mercari_db import create_driver

def scrape_item_detail(driver, url: str):
    """
    Yahoo!ショッピングの商品ページから詳細情報を取得する
    """
    try:
        driver.get(url)
    except Exception as e:
        print(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error", 
            "description": "", "image_urls": [], "variants": []
        }

    wait = WebDriverWait(driver, 10)
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2) # Wait for dynamic loading
    except Exception:
        pass

    # ---- Title ----
    title = ""
    for selector in [".mdItemName", ".elName", "h1", "[data-testid='item-name']"]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            if els:
                title = els[0].text.strip()
                if title: break
        except:
            continue

    # ---- Price ----
    price = None
    for selector in [".mdItemPrice", ".elPrice", ".elItemPrice", "[data-testid='item-price']"]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            if els:
                txt = els[0].text
                m = re.search(r"([\d,]+)", txt)
                if m:
                    price = int(m.group(1).replace(",", ""))
                    break
        except:
            continue
            
    # Fallback price from body if needed (risky but useful)
    if price is None:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            m = re.search(r"([\d,]+)\s*円", body_text)
            if m:
                price = int(m.group(1).replace(",", ""))
        except:
            pass

    # ---- Description ----
    description = ""
    # Yahoo tends to put description in iframes or specific divs
    # We will try to get the main text content roughly
    desc_selectors = [".mdItemDescription", ".elItemInfo", "#item-info"]
    for sel in desc_selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                description = els[0].text.strip()
                break
        except:
            continue
            
    if not description:
        # Fallback to meta description
        try:
            meta = driver.find_element(By.CSS_SELECTOR, "meta[name='description']")
            description = meta.get_attribute("content")
        except:
            pass

    # ---- Images ----
    image_urls = []
    try:
        # Main images often in a slider or list
        img_selectors = [
            ".mdItemImage img", ".elItemImage img", 
            ".libItemImage img", "#item-image img"
        ]
        found_imgs = []
        for sel in img_selectors:
            found_imgs.extend(driver.find_elements(By.CSS_SELECTOR, sel))
            
        for img in found_imgs:
            src = img.get_attribute("src")
            # Filter low res or icons
            if src and "http" in src and "y-img.jp" in src:
                # Yahoo thumbnails often have '_A_' or similar, try to get larger if possible?
                # For now just grab what we see
                if src not in image_urls:
                    image_urls.append(src)
    except:
        pass

    # ---- Status (Simplified) ----
    status = "on_sale"
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "在庫切れ" in body_text or "売り切れ" in body_text:
            status = "sold"
    except:
        pass

    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": [] # Variants are hard on Yahoo (often iframes), skipping for MVP
    }

def scrape_single_item(url: str, headless: bool = True):
    """
    One-shot scraping for Yahoo Shopping
    """
    driver = None
    try:
        print(f"DEBUG: Starting Yahoo scrape for {url}")
        driver = create_driver(headless=headless)
        data = scrape_item_detail(driver, url)
        if data["title"]:
            print(f"DEBUG: Success -> {data['title']}")
        return [data]
    except Exception as e:
        print(f"Yahoo Scrape Error: {e}")
        return []
    finally:
        if driver:
            driver.quit()
