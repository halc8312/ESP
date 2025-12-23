import logging
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import re
import os
import shutil
import uuid

# Import selector configuration loader
try:
    from selector_config import get_selectors
except ImportError:
    # Fallback if module not found
    def get_selectors(site, page_type, field):
        return []

# Import metrics logging
try:
    from scrape_metrics import get_metrics, log_scrape_result, check_scrape_health
except ImportError:
    # Fallback if module not found
    def get_metrics():
        class DummyMetrics:
            def start(self, *a): pass
            def record_attempt(self, *a): pass
            def finish(self): return {}
        return DummyMetrics()
    def log_scrape_result(*a): return True
    def check_scrape_health(*a): return {"action_required": False}

def create_driver(headless: bool = True):
    """Chrome WebDriver を生成（Render/Docker 低メモリ環境最適化版）"""
    options = Options()

    # --- Docker環境で必須の設定 ---
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    # --- ヘッドレスモードの設定 ---
    if headless:
        options.add_argument("--headless=new")

    # --- Bot検知対策 ---
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36'
    options.add_argument(f'user-agent={user_agent}')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        logging.error(f"Failed to initialize WebDriver: {e}")
        raise e


def scrape_shops_product(driver, url: str):
    """メルカリShopsの商品ページ用スクレイピング"""
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
        time.sleep(2) # Shopsはロードが遅いことがあるため待機
    except Exception:
        pass

    # ---- タイトル ----
    title = ""
    title_selectors = get_selectors('mercari', 'shops', 'title') or ["[data-testid='product-name']", "h1"]
    try:
        for selector in title_selectors:
            title_el = driver.find_elements(By.CSS_SELECTOR, selector)
            if title_el:
                title = title_el[0].text.strip()
                break
    except Exception:
        pass

    # ---- 価格 ----
    price = None
    price_selectors = get_selectors('mercari', 'shops', 'price') or ["[data-testid='product-price']"]
    try:
        for selector in price_selectors:
            price_els = driver.find_elements(By.CSS_SELECTOR, selector)
            if price_els:
                price_text = price_els[0].text
                m = re.search(r"([\d,]+)", price_text)
                if m:
                    price = int(m.group(1).replace(",", ""))
                    break
    except Exception:
        pass
    
    # 予備の価格取得ロジック
    if price is None:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            m = re.search(r"([\d,]+)\s*円", body_text)
            if m:
                price = int(m.group(1).replace(",", ""))
        except Exception:
            pass

    # ---- 説明文 ----
    description = ""
    desc_selectors = get_selectors('mercari', 'shops', 'description') or ["[data-testid='product-description']"]
    try:
        for selector in desc_selectors:
            desc_els = driver.find_elements(By.CSS_SELECTOR, selector)
            if desc_els:
                description = desc_els[0].text.strip()
                break
    except Exception:
        pass

    # ---- 画像 ----
    image_urls = []
    image_selectors = get_selectors('mercari', 'shops', 'images') or ["img[src*='mercari'][src*='static']"]
    try:
        for selector in image_selectors:
            imgs = driver.find_elements(By.CSS_SELECTOR, selector)
            for img in imgs:
                src = img.get_attribute("src")
                if src and src not in image_urls:
                    image_urls.append(src)
    except Exception:
        pass

    # ---- バリエーション（簡易取得） ----
    variants = []
    item_data_update = {}
    try:
        # トラブルシューティング: Shopsのバリエーション取得 (DOM構造解析に基づく)
        # ラベル(span/p) -> 親(div) -> 兄弟(div) -> 子要素(div/a) 
        # テキストには価格や在庫情報が含まれるためクリーニングが必要
        def extract_options(label_texts):
            found_options = []
            for label_text in label_texts:
                xpath = f"//*[contains(text(), '{label_text}')]"
                labels = driver.find_elements(By.XPATH, xpath)
                
                for label in labels:
                    try:
                        # Skip if script or style
                        if label.tag_name in ['script', 'style']: continue
                        
                        # 親の兄弟要素（コンテナ）を探す
                        parent = label.find_element(By.XPATH, "..")
                        container = driver.execute_script("return arguments[0].nextElementSibling", parent)
                        
                        if container:
                            # コンテナ内の直下の子要素をオプション候補とする
                            children = container.find_elements(By.XPATH, "./*")
                            if children:
                                for child in children:
                                    raw_text = child.text.strip()
                                    if not raw_text: continue
                                    
                                    # 1行目を取得（価格や在庫情報は改行されることが多いが、念のためRegexで掃除）
                                    val = raw_text.split('\n')[0].strip()
                                    
                                    # Regex cleaning
                                    val = re.sub(r'[¥￥]\s*[\d,]+', '', val) # Remove price
                                    val = re.sub(r'[\d,]+\s*円', '', val)    # Remove price
                                    val = re.sub(r'残り\d+点', '', val)      # Remove stock info
                                    val = re.sub(r'売り切れ', '', val)       # Remove status
                                    val = re.sub(r'在庫なし', '', val)       # Remove status
                                    val = val.strip()

                                    # いいね！ボタンなどを除外
                                    if "いいね" in val or "シェア" in val or "もっと見る" in val or not val: continue
                                    
                                    if val and val not in found_options:
                                        found_options.append(val)
                                
                                if found_options:
                                    return found_options
                    except:
                        continue
            return found_options

        colors = extract_options(['カラー', 'Color'])
        types = extract_options(['種類', 'サイズ', 'Size'])

        print(f"DEBUG Colors found: {colors}")
        print(f"DEBUG Types found: {types}")

        # 色と種類を組み合わせてバリエーションを作成
        if colors and types:
            option1_name = "カラー"
            option2_name = "サイズ/種類"
            for color in colors:
                for type_val in types:
                    variants.append({
                        "option1_name": option1_name,
                        "option1_value": color,
                        "option2_name": option2_name,
                        "option2_value": type_val,
                        "price": price,
                        "inventory_qty": 1 
                    })
            item_data_update = {"option1_name": option1_name, "option2_name": option2_name}
            
        elif colors: # 色だけの場合
            option1_name = "カラー"
            for color in colors:
                variants.append({
                    "option1_name": option1_name,
                    "option1_value": color,
                    "price": price,
                    "inventory_qty": 1
                })
            item_data_update = {"option1_name": option1_name}

        elif types: # 種類だけの場合
            option1_name = "種類" # デフォルト
            for type_val in types:
                variants.append({
                    "option1_name": option1_name,
                    "option1_value": type_val,
                    "price": price,
                    "inventory_qty": 1
                })
            item_data_update = {"option1_name": option1_name}

    except Exception as e:
        print(f"Error extracting variants: {e}")
        pass
    
    # ---- ステータス ----
    status = "on_sale"
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "売り切れ" in body_text or "在庫なし" in body_text:
            status = "sold"
    except Exception:
        pass

    # Update item_data with found option names
    item_data = {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": variants
    }
    item_data.update(item_data_update)
    return item_data




def scrape_item_detail(driver, url: str):
    """1つの商品ページから詳細情報を取得して dict で返す"""
    
    # Shops URL判定
    if "/shops/product/" in url:
        return scrape_shops_product(driver, url)

    try:
        driver.get(url)
    except Exception as e:
        print(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error", 
            "description": "", "image_urls": [], "variants": []
        }

    wait = WebDriverWait(driver, 10)

    # eagerモードなのでbodyの出現を軽く待つ
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception:
        pass

    # ---- タイトル ----
    try:
        title_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "h1")))
        title = title_el.text.strip()
    except Exception:
        title = ""

    # ---- ページ全体のテキスト ----
    try:
        body_el = driver.find_element(By.TAG_NAME, "body")
        body_text = body_el.text
    except Exception:
        body_text = ""

    # ---- 価格 ----
    price = None
    price_selectors = get_selectors('mercari', 'general', 'price') or ["[data-testid='price']"]
    try:
        # メルカリのクラス名は頻繁に変わるため、data-testidがあれば優先
        for selector in price_selectors:
            price_el = driver.find_elements(By.CSS_SELECTOR, selector)
            if price_el:
                price_text = price_el[0].text
                m = re.search(r"[¥￥]\s*([\d,]+)", price_text) or re.search(r"([\d,]+)", price_text)
                if m:
                    price = int(m.group(1).replace(",", ""))
                    break
    except Exception:
        pass

    if price is None and body_text:
        m = re.search(r"[¥￥]\s*([\d,]+)", body_text)
        if not m:
            m = re.search(r"([\d,]+)\s*円", body_text)
        if m:
            try:
                price = int(m.group(1).replace(",", ""))
            except ValueError:
                price = None

    # ---- ステータス ----
    status = "unknown"
    try:
        if "売り切れ" in body_text or "Sold" in body_text:
             # ボタンチェックも念のため
             sold_btns = driver.find_elements(By.XPATH, "//button[contains(., '売り切れ')]")
             if sold_btns:
                 status = "sold"
        
        if status == "unknown" and ("購入手続きへ" in body_text or "Buy this item" in body_text):
             status = "on_sale"
    except Exception:
        pass

    # ---- 商品説明 ----
    description = ""
    try:
        if "商品の説明" in body_text:
            after = body_text.split("商品の説明", 1)[1]
            end_pos = len(after)
            for marker in ["商品の情報", "商品情報", "出品者", "コメント"]:
                idx = after.find(marker)
                if idx != -1 and idx < end_pos:
                    end_pos = idx
            description = after[:end_pos].strip()
    except Exception as e:
        logging.debug("Failed to extract description: %s", e)
        description = body_text[:200]

    # ---- 商品画像 ----
    image_urls = []
    image_selectors = get_selectors('mercari', 'general', 'images') or [
        "img[src*='static.mercdn.net'][src*='/item/'][src*='/photos/']"
    ]
    try:
        # 画像取得も少し待つ
        time.sleep(1)
        for selector in image_selectors:
            img_elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for img in img_elements:
                src = img.get_attribute("src")
                if src and src not in image_urls:
                    image_urls.append(src)
    except Exception:
        pass

    # ---- バリエーション（一般出品） ----
    variants = []
    try:
        # 一般出品は「商品の情報」欄などにサイズや色が書かれているが、
        # 選択式のUI（ボタン）がある場合はそれを優先
        selectors = [
             "mer-item-thumbnail ~ div button", # 新UI
             "//div[contains(text(), '種類')]/..//button",
             "//div[contains(text(), 'サイズ')]/..//button",
             "//div[contains(text(), 'カラー')]/..//button",
             "//p[contains(text(), 'サイズ')]/..//button",
             "//p[contains(text(), 'カラー')]/..//button",
             "button[aria-haspopup='listbox']",
             "div[role='radiogroup'] div[role='radio']",
             "div[data-testid='product-variant-selector'] button"
        ]
        
        found_elements = []
        for sel in selectors:
            if sel.startswith("//"):
                found_elements = driver.find_elements(By.XPATH, sel)
            else:
                found_elements = driver.find_elements(By.CSS_SELECTOR, sel)
            
            if found_elements and len(found_elements) > 1:
                break
                
        seen_opts = set()
        for el in found_elements:
            text_val = el.text.strip()
            if text_val and text_val not in seen_opts:
                seen_opts.add(text_val)
                variants.append({
                    "option1_value": text_val,
                    "price": price, 
                    "inventory_qty": 1
                })
    except Exception:
        pass

    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": variants
    }


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
):
    """
    メルカリ検索URLから複数商品をスクレイピングして list[dict] を返す。
    """
    driver = None
    try:
        print("DEBUG: Starting scrape_search_result (Low Memory Config)")
        driver = create_driver(headless=headless)
        items = []

        print(f"DEBUG: Navigating to {search_url}")
        driver.get(search_url)
        print(f"DEBUG: Page Title = {driver.title}")
        
        # ページロード戦略をeagerにしたので、bodyが出るまで明示的に少し待つ
        wait = WebDriverWait(driver, 15)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            print("DEBUG: Timeout waiting for body")

        # Try to collect enough links to satisfy max_items
        links = []
        scroll_attempts = 0
        while len(links) < max_items * 2 and scroll_attempts < max_scroll * 2: # Fetch more potential links
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            
            new_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")
            if not new_links:
                new_links = driver.find_elements(By.CSS_SELECTOR, "li[data-testid='item-cell'] a")
            
            if not new_links:
                # No items found via either selector, stop scrolling/searching
                break

            # Simple dedup based on current view
            current_hrefs = {l.get_attribute("href") for l in links}
            for nl in new_links:
                h = nl.get_attribute("href")
                if h and "/item/" in h and h not in current_hrefs:
                    links.append(nl)
            
            if len(links) >= max_items * 1.5: # buffer
                break
                
            scroll_attempts += 1

        print(f"DEBUG: Found {len(links)} unique links on search page.")
        
        item_urls = []
        seen = set()
        for link in links:
            href = link.get_attribute("href")
            if not href or "/item/" not in href or href in seen:
                continue
            seen.add(href)
            item_urls.append(href)

        filtered_items = []
        for url in item_urls:
            if len(filtered_items) >= max_items:
                break
                
            print(f"DEBUG: Scraping item {url}")
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
        print(f"CRITICAL ERROR during scraping: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e:
                logging.debug("Error quitting driver: %s", e)


def scrape_single_item(url: str, headless: bool = True):
    """
    指定された商品URLを1件だけスクレイピングして list[dict] を返す。
    save_scraped_items_to_db にそのまま渡せるようにリストに包んでいる。
    """
    driver = None
    metrics = get_metrics()
    metrics.start('mercari', 'single')
    try:
        print(f"DEBUG: Starting scrape_single_item for {url}")
        driver = create_driver(headless=headless)
        
        data = scrape_item_detail(driver, url)
        log_scrape_result('mercari', url, data)
        
        if data["title"]:
            print(f"DEBUG: Success -> {data['title']}")
        else:
            print("DEBUG: Failed to get title")

        metrics.finish()
        return [data]

    except Exception as e:
        print(f"CRITICAL ERROR during single scraping: {e}")
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
