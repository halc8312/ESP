"""
Surugaya scraper - Product detail scraping for suruga-ya.jp
Uses curl_cffi to bypass Cloudflare protection by impersonating Chrome's TLS fingerprint.
"""
import logging
import re
from bs4 import BeautifulSoup
from curl_cffi import requests

logger = logging.getLogger("surugaya")

# CSS Selectors
SELECTORS = {
    "title": "h1",
    "price": ".price_group .text-price-detail, .price_group label, span.text-price-detail",
    "stock_available": ".btn_buy, .cart1, #cart-add",
    "stock_sold": ".waitbtn, .soldout",
    "main_image": ".is-main-image img, #item_picture",
    "description": ".tbl_product_info, #item_condition",
    "condition": ".price_group label, .condition",
    "category": ".left div a[href*='category='], .breadcrumb a",
}

def get_session():
    """Create a curl_cffi session that impersonates Chrome."""
    session = requests.Session(impersonate="chrome120")
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    })
    return session

def scrape_item_detail(session, url: str) -> dict:
    """
    駿河屋の商品ページから詳細情報を取得する (curl_cffi版)
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
    
    print(f"[SURUGAYA] Starting curl_cffi scrape for {url}")
    
    try:
        resp = session.get(url, timeout=30)
        
        # Check for Cloudflare block
        if resp.status_code in [403, 503] or "Just a moment" in resp.text or "attention required" in resp.text.lower():
            print(f"[SURUGAYA] ERROR: Cloudflare block detected (Status: {resp.status_code})")
            result["status"] = "blocked"
            return result
            
        soup = BeautifulSoup(resp.content, "html.parser")
        
    except Exception as e:
        print(f"[SURUGAYA] ERROR during page load: {e}")
        result["status"] = "error"
        return result
    
    # ---- タイトル ----
    title_el = soup.select_one(SELECTORS["title"])
    if title_el:
        result["title"] = title_el.get_text(strip=True)
    
    # ---- 価格 ----
    price_els = soup.select(SELECTORS["price"])
    for el in price_els:
        text = el.get_text(strip=True)
        # Extract price from "中古 3,700円 (税込)" format
        match = re.search(r"([\d,]+)\s*円", text)
        if match:
            result["price"] = int(match.group(1).replace(",", ""))
            break
            
    # Fallback: search body text
    if result["price"] is None:
        match = re.search(r"([\d,]+)\s*円\s*\(税込\)", soup.get_text())
        if match:
            result["price"] = int(match.group(1).replace(",", ""))
    
    # ---- 在庫状態 ----
    buy_btn = soup.select(SELECTORS["stock_available"])
    sold_btn = soup.select(SELECTORS["stock_sold"])
    
    if buy_btn:
        result["status"] = "active"
    elif sold_btn:
        result["status"] = "sold"
    else:
        if "品切れ" in soup.get_text():
            result["status"] = "sold"
        else:
            result["status"] = "active"
    
    # ---- 商品状態（中古/新品）----
    condition_els = soup.select(SELECTORS["condition"])
    for el in condition_els:
        text = el.get_text(strip=True)
        if "中古" in text:
            result["condition"] = "中古"
            break
        elif "新品" in text:
            result["condition"] = "新品"
            break
            
    # ---- 画像 ----
    img_el = soup.select_one(SELECTORS["main_image"])
    if img_el and img_el.has_attr('src'):
        result["image_urls"].append(img_el['src'])
        
    # ---- 説明（#product_detail テーブル）----
    detail_el = soup.select_one(SELECTORS["description"])
    if detail_el:
        result["description"] = detail_el.get_text(separator="\n", strip=True)
        
    # ---- カテゴリ ----
    category_els = soup.select(SELECTORS["category"])
    if category_els:
        categories = [el.get_text(strip=True) for el in category_els if el.get_text(strip=True)]
        result["category"] = " > ".join(categories)
        
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
    try:
        session = get_session()
        result = scrape_item_detail(session, url)
        return [result] if result["title"] else []
    except Exception as e:
        print(f"[SURUGAYA] Error in scrape_single_item: {e}")
        return []


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
) -> list:
    """
    駿河屋検索結果から複数商品をスクレイピング (curl_cffi版)
    """
    results = []
    
    try:
        print(f"[SURUGAYA] Search: Initializing curl_cffi session...")
        session = get_session()
        
        print(f"[SURUGAYA] Search: Fetching {search_url}")
        resp = session.get(search_url, timeout=30)
        
        if resp.status_code in [403, 503] or "Just a moment" in resp.text or "attention required" in resp.text.lower():
            print(f"[SURUGAYA] Search: ERROR: Cloudflare block detected (Status: {resp.status_code})")
            return results
            
        soup = BeautifulSoup(resp.content, "html.parser")
        print(f"[SURUGAYA] Search: Page title: {soup.title.string if soup.title else 'No Title'}")
        
        # Find product links
        product_urls = set()
        for a in soup.select(".item a[href*='/product/detail/'], a[href*='/product/detail/']"):
            href = a.get('href')
            if href and "/product/detail/" in href:
                # Ensure absolute URL
                if href.startswith('/'):
                    href = f"https://www.suruga-ya.jp{href}"
                product_urls.add(href)
                if len(product_urls) >= max_items:
                    break
                    
        # Scrape each product
        for url in list(product_urls)[:max_items]:
            try:
                result = scrape_item_detail(session, url)
                if result["title"]:
                    results.append(result)
            except Exception as e:
                logger.error(f"Error scraping {url}: {e}")
                continue
                
        return results
        
    except Exception as e:
        logger.error(f"Error in scrape_search_result: {e}")
        return results
