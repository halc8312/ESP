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
    # Try more generic selectors first as Yahoo has many templates
    # .mdItemName, .elName are common classes
    for selector in [
        ".mdItemName", ".elName", 
        "h1.title", "h1.name", 
        "[data-testid='item-name']", 
        "h1" # Last resort
    ]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            if els:
                # Yahoo titles sometimes contain newlines or extra spaces
                t = els[0].text.replace('\n', ' ').strip()
                if t: 
                    title = t
                    break
        except:
            continue
            
    # If title is still empty, try meta title
    if not title:
        try:
            title = driver.title.split('-')[0].strip()
        except:
            pass

    # ---- Price ----
    price = None
    for selector in [
        ".mdItemPrice", ".elPrice", ".elItemPrice", 
        "[data-testid='item-price']",
        ".price" # Generic fallbacks
    ]:
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
    desc_selectors = [
        ".mdItemDescription", ".elItemInfo", "#item-info",
        ".explanation", ".item_exp" # Older templates
    ]
    for sel in desc_selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                description = els[0].text.strip()
                break
        except:
            continue
            
    if not description:
        try:
            meta = driver.find_element(By.CSS_SELECTOR, "meta[name='description']")
            description = meta.get_attribute("content")
        except:
            pass

    # ---- Images ----
    image_urls = []
    try:
        # 1. Look for main image container first
        # Yahoo often uses .mdItemImage or .elItemImage
        # Also check for "item_image" id
        
        candidates = []
        
        # Helper to collect images from a container
        def collect_imgs(selector):
            try:
                els = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in els:
                    src = el.get_attribute("src")
                    # Try to get high-res if available in data attributes
                    if not src:
                        src = el.get_attribute("data-src") or el.get_attribute("data-original")
                    
                    if src: candidates.append(src)
            except:
                pass

        collect_imgs(".mdItemImage img")
        collect_imgs(".elItemImage img")
        collect_imgs(".libItemImage img")
        collect_imgs("#item-image img")
        collect_imgs("ul.elItemImage > li > img") # Slider thumbnails often imply main images exist
        
        # If nothing specific found, get all images that look like product photos
        if not candidates:
            all_imgs = driver.find_elements(By.TAG_NAME, "img")
            for img in all_imgs:
                src = img.get_attribute("src")
                if src and ("y-img.jp" in src or "shopping.c.yimg.jp" in src):
                    candidates.append(src)
        
        for src in candidates:
            # Filter logic
            if not src: continue
            if "icon" in src or "blank" in src or "logo" in src: continue
            
            # Clean up Yahoo image URLs if possible (remove standard resizing params?)
            # Yahoo URL example: https://item-shopping.c.yimg.jp/i/n/shopname_itemcode
            # Thumbnails: .../i/g/... or .../i/l/... ? /n/ is usually main.
            
            if src not in image_urls:
                image_urls.append(src)
                
    except Exception as e:
        print(f"Image scrape error: {e}")
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


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3, # Not used for Yahoo pagination but kept for interface consistency
    headless: bool = True,
):
    """
    Yahoo! Shopping 検索結果から複数商品をスクレイピングする
    """
    driver = None
    try:
        print(f"DEBUG: Starting Yahoo search scrape for {search_url}")
        driver = create_driver(headless=headless)
        items = []

        driver.get(search_url)
        wait = WebDriverWait(driver, 15)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except:
            pass

        # Collect links
        links = []
        # Yahoo Search Result Selectors
        # Standard: .LoopList__item a
        # Grid: .Item__title a
        # List: .elTitle a
        
        # Try to collect enough unique item links
        page = 1
        while len(links) < max_items:
            # Get links on current page
            candidates = []
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, "li.LoopList__item a"))
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, ".Item__title a"))
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, "[data-testid='item-name'] a"))
            
             # Deduplicate on page
            for cand in candidates:
                href = cand.get_attribute("href")
                if href and "store.shopping.yahoo.co.jp" in href and href not in [l.get_attribute("href") for l in links]:
                    links.append(cand)
            
            if len(links) >= max_items:
                break
                
            # Pagination Logic (Next Page)
            try:
                # Yahoo pagination 'Next' often has class .elNext or text '次へ' or '>'
                next_btn = driver.find_elements(By.CSS_SELECTOR, "a.elNext")
                if not next_btn:
                     next_btn = driver.find_elements(By.XPATH, "//a[contains(text(), '次へ')]")
                
                if next_btn:
                    print(f"DEBUG: Navigating to next page {page+1}")
                    next_btn[0].click()
                    time.sleep(3)
                    page += 1
                else:
                    break # No more pages
            except:
                break

        print(f"DEBUG: Found {len(links)} links. Scraping top {max_items}...")
        
        # Scrape details
        # Note: We can't use the 'links' elements directly effectively after navigation, 
        # so we should store URLs first.
        target_urls = [l.get_attribute("href") for l in links][:max_items]
        
        for url in target_urls:
            print(f"DEBUG: Scraping {url}")
            try:
                data = scrape_item_detail(driver, url)
                if data["title"]:
                    print(f"DEBUG: Success -> {data['title']}")
                    items.append(data)
                time.sleep(1)
            except Exception as e:
                 print(f"Error scraping {url}: {e}")
        
        return items

    except Exception as e:
        print(f"Yahoo Search Error: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if driver:
            driver.quit()
