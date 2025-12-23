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
    # Load selectors from config (with fallback to hardcoded if config not found)
    title_selectors = get_selectors('yahoo', 'detail', 'title') or [
        "[class*='styles_itemName']", "[class*='styles_itemTitle']",
        ".mdItemName", ".elName", "h1.title", "h1.name", 
        "[data-testid='item-name']", "h1"
    ]
    for selector in title_selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            if els:
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
    price_selectors = get_selectors('yahoo', 'detail', 'price') or [
        "[class*='styles_price']", ".mdItemPrice", ".elPrice", 
        ".elItemPrice", "[data-testid='item-price']", ".price"
    ]
    for selector in price_selectors:
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
    desc_selectors = get_selectors('yahoo', 'detail', 'description') or [
        "[class*='styles_itemDescription']",
        ".mdItemDescription", ".elItemInfo", "#item-info",
        ".explanation", ".item_exp"
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

        # Load image selectors from config
        image_selectors = get_selectors('yahoo', 'detail', 'images') or [
            "[class*='styles_image'] img", "[class*='styles_mainImage'] img",
            ".mdItemImage img", ".elItemImage img", ".libItemImage img",
            "#item-image img", "ul.elItemImage > li > img"
        ]
        for selector in image_selectors:
            collect_imgs(selector)
        
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
    metrics = get_metrics()
    metrics.start('yahoo', 'single')
    try:
        print(f"DEBUG: Starting Yahoo scrape for {url}")
        driver = create_driver(headless=headless)
        data = scrape_item_detail(driver, url)
        log_scrape_result('yahoo', url, data)
        if data["title"]:
            print(f"DEBUG: Success -> {data['title']}")
        metrics.finish()
        return [data]
    except Exception as e:
        print(f"Yahoo Scrape Error: {e}")
        metrics.record_attempt(False, url, str(e))
        metrics.finish()
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
    metrics = get_metrics()
    metrics.start('yahoo', 'search')
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
        # Load search result selectors from config
        item_link_selectors = get_selectors('yahoo', 'search', 'item_links') or [
            "a[class*='SearchResult_SearchResultItem__detailLink']",
            "a[class*='ItemImageLink']",
            "li.LoopList__item a", ".Item__title a", "[data-testid='item-name'] a"
        ]
        valid_domains = get_valid_domains('yahoo', 'search') or [
            "store.shopping.yahoo.co.jp", "shopping-item-reach.yahoo.co.jp"
        ]
        
        # Try to collect enough unique item links
        page = 1
        while len(links) < max_items:
            # Get links on current page using selectors from config
            candidates = []
            for selector in item_link_selectors:
                candidates.extend(driver.find_elements(By.CSS_SELECTOR, selector))
            
             # Deduplicate on page
            for cand in candidates:
                href = cand.get_attribute("href")
                if href and any(domain in href for domain in valid_domains) and href not in [l.get_attribute("href") for l in links]:
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
                log_scrape_result('yahoo', url, data)
                if data["title"]:
                    print(f"DEBUG: Success -> {data['title']}")
                    items.append(data)
                time.sleep(1)
            except Exception as e:
                metrics.record_attempt(False, url, str(e))
                print(f"Error scraping {url}: {e}")
        
        # Check health and log final metrics
        health = check_scrape_health(items)
        if health['action_required']:
            logging.warning(f"Yahoo scrape health check: {health['message']}")
        metrics.finish()
        
        return items

    except Exception as e:
        print(f"Yahoo Search Error: {e}")
        import traceback
        traceback.print_exc()
        metrics.finish()
        return []
    finally:
        if driver:
            driver.quit()
