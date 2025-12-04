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

def create_driver(headless: bool = True):
    """Chrome WebDriver を生成（Render/Docker 低メモリ環境最適化版）"""
    options = Options()

    # --- Docker環境で必須の設定 ---
    # サンドボックス化を無効化（Docker内では権限の問題で必須）
    options.add_argument("--no-sandbox")
    # 共有メモリの使用を無効化（/dev/shmのサイズ制限によるクラッシュを回避）
    options.add_argument("--disable-dev-shm-usage")
    # GPU無効化（Linuxサーバー環境での安定性向上）
    options.add_argument("--disable-gpu")
    
    # --- ヘッドレスモードの設定 ---
    if headless:
        options.add_argument("--headless=new")

    # --- その他、安定性のための設定（任意） ---
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")
    
    # --- 重要: バイナリパスの指定 ---
    # Dockerfileでインストールした場合、通常は自動検出されますが、
    # もしエラーが出る場合は以下のように明示的に指定する場合もあります。
    # options.binary_location = "/usr/bin/google-chrome"

    try:
        # Docker内のChromeバージョンに合わせてドライバを自動インストール
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        logging.error(f"Failed to initialize WebDriver: {e}")
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
    try:
        # メルカリのクラス名は頻繁に変わるため、data-testidがあれば優先
        price_el = driver.find_elements(By.CSS_SELECTOR, "[data-testid='price']")
        if price_el:
            price_text = price_el[0].text
            m = re.search(r"[¥￥]\s*([\d,]+)", price_text) or re.search(r"([\d,]+)", price_text)
            if m:
                price = int(m.group(1).replace(",", ""))
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
    try:
        # 画像取得も少し待つ
        time.sleep(1) 
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

        for i in range(max_scroll):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2) # スクロール待機は少し短くてもOK

        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")
        if len(links) == 0:
             print("DEBUG: No links found, trying list selector...")
             links = driver.find_elements(By.CSS_SELECTOR, "li[data-testid='item-cell'] a")

        print(f"DEBUG: Found {len(links)} links on search page.")

        item_urls = []
        seen = set()

        for link in links:
            href = link.get_attribute("href")
            if not href or "/item/" not in href or href in seen:
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
                 print("DEBUG: Failed to get title")
            
            items.append(data)
            time.sleep(1) # 負荷対策

        return items

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
