from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import os
import shutil
import uuid

def create_driver(headless: bool = True):
    """Chrome WebDriver を生成（Render/Docker環境 安定版）"""
    options = Options()

    # --- 基本設定 ---
    if headless:
        options.add_argument("--headless=new")

    # --- 必須オプション（これだけで動くことが多い）---
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    # ★重要: クラッシュの原因になるため single-process は削除しました
    
    # --- 安定化・エラー回避 ---
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--window-size=1280,1024")
    
    # --- ボット対策回避（User-Agent偽装）---
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    options.add_argument(f'--user-agent={user_agent}')
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    # --- ユーザーデータディレクトリ（競合回避） ---
    user_data_dir = f"/tmp/chrome_data_{uuid.uuid4()}"
    options.add_argument(f"--user-data-dir={user_data_dir}")

    # --- バイナリ場所の自動探索 ---
    # 環境変数を優先しつつ、なければ標準パスを探す
    binary_candidates = [
        os.environ.get("CHROME_BINARY_LOCATION"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/google-chrome"
    ]
    
    binary_found = False
    for path in binary_candidates:
        if path and os.path.exists(path):
            options.binary_location = path
            print(f"DEBUG: Found Chrome binary at {path}")
            binary_found = True
            break
    
    if not binary_found:
        print("DEBUG: Chrome binary not found in standard paths. Letting Selenium auto-detect.")

    # --- ログ設定（エラー時の詳細用） ---
    service = Service(ChromeDriverManager().install())
    
    # 起動試行
    try:
        driver = webdriver.Chrome(service=service, options=options)
        # navigator.webdriver フラグを消す
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return driver
    except Exception as e:
        print(f"CRITICAL ERROR in create_driver: {e}")
        try:
            shutil.rmtree(user_data_dir)
        except:
            pass
        raise e


def scrape_item_detail(driver, url: str):
    """1つの商品ページから詳細情報を取得して dict で返す"""
    try:
        driver.get(url)
    except Exception as e:
        print(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error", 
            "description": "", "image_urls": []
        }

    wait = WebDriverWait(driver, 10)

    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(1)
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
    try:
        price_el = driver.find_element(By.CSS_SELECTOR, "[data-testid='price']")
        price_text = price_el.text
        m = re.search(r"[¥￥]\s*([\d,]+)", price_text)
        if not m:
            m = re.search(r"([\d,]+)", price_text)
        if m:
            price = int(m.group(1).replace(",", ""))
    except Exception:
        price = None

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
        sold_btns = driver.find_elements(By.XPATH, "//button[contains(., '売り切れ')]")
        buy_btns = driver.find_elements(By.XPATH, "//button[contains(., '購入手続きへ')]")
        if sold_btns:
            status = "sold"
        elif buy_btns:
            status = "on_sale"
    except Exception:
        pass

    if status == "unknown" and body_text:
        if "売り切れました" in body_text:
            status = "sold"
        elif "購入手続きへ" in body_text:
            status = "on_sale"

    # ---- 商品説明 ----
    description = ""
    if "商品の説明" in body_text:
        try:
            after = body_text.split("商品の説明", 1)[1]
            end_pos = len(after)
            for marker in ["商品の情報", "商品情報", "商品の特徴", "出品者", "コメント ("]:
                idx = after.find(marker)
                if idx != -1 and idx < end_pos:
                    end_pos = idx
            desc_block = after[:end_pos].strip()
            lines = [ln.strip() for ln in desc_block.splitlines() if ln.strip()]
            cleaned_lines = []
            for ln in lines:
                if any(x in ln for x in ["分前", "時間前", "日前"]):
                    continue
                cleaned_lines.append(ln)
            description = "\n".join(cleaned_lines)
        except:
            description = body_text[:200]

    # ---- 商品画像 ----
    image_urls = []
    try:
        img_elements = driver.find_elements(
            By.CSS_SELECTOR,
            "img[src*='static.mercdn.net'][src*='/item/'][src*='/photos/']",
        )
        for img in img_elements:
            src = img.get_attribute("src")
            if src and src not in image_urls:
                image_urls.append(src)
    except Exception:
        pass

    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
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
        print("DEBUG: Starting scrape_search_result")
        driver = create_driver(headless=headless)
        items = []

        print(f"DEBUG: Navigating to {search_url}")
        driver.get(search_url)
        print(f"DEBUG: Page Title = {driver.title}")
        
        wait = WebDriverWait(driver, 15)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            print("DEBUG: Timeout waiting for body")

        for i in range(max_scroll):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)

        # リンク取得
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")
        if len(links) == 0:
             print("DEBUG: No links found with 'a[href*=/item/]', trying list selector...")
             links = driver.find_elements(By.CSS_SELECTOR, "li[data-testid='item-cell'] a")

        print(f"DEBUG: Found {len(links)} links on search page.")

        item_urls = []
        seen = set()

        for link in links:
            href = link.get_attribute("href")
            if not href:
                continue
            if "/item/" not in href:
                continue
            if href in seen:
                continue

            seen.add(href)
            item_urls.append(href)

            if len(item_urls) >= max_items:
                break

        print(f"DEBUG: Going to scrape {len(item_urls)} items.")

        for url in item_urls:
            print(f"DEBUG: Scraping item {url}")
            data = scrape_item_detail(driver, url)
            if data["title"]: 
                 print(f"DEBUG: Success -> {data['title']}")
            else:
                 print("DEBUG: Failed to get title (Bot block?)")
            
            items.append(data)
            time.sleep(3)

        return items

    except Exception as e:
        print(f"CRITICAL ERROR during scraping: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if driver:
            try:
                # user-data-dir の掃除用
                user_data_dir = None
                for arg in driver.capabilities.get('chrome', {}).get('userDataDir', ''):
                    pass
                driver.quit()
            except:
                pass
