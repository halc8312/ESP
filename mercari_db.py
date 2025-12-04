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
import uuid  # ★追加: ディレクトリ名のランダム化に使用

def create_driver(headless: bool = True):
    """Chrome WebDriver を生成（Render/Docker環境 完全対策 + UA偽装版）"""
    options = Options()

    # --- 基本設定 ---
    if headless:
        options.add_argument("--headless=new")

    # --- ボット対策回避（User-Agent偽装） ---
    # 一般的なWindows PCのChromeになりすます
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    options.add_argument(f'--user-agent={user_agent}')

    # --- Docker/Renderでのクラッシュを防ぐ必須オプション群 ---
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--no-sandbox")
    
    # ★重要: メモリの少ないコンテナ環境でのクラッシュを防ぐ
    options.add_argument("--single-process")
    options.add_argument("--disable-zygote")
    
    # --- グラフィック・機能制限 ---
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    
    # --- ネットワーク・ポート設定 ---
    # ★修正: ポート指定(--remote-debugging-port)は削除しました。
    # 競合してクラッシュする原因になるため、自動割り当てに任せます。
    
    options.add_argument("--window-size=1280,1024")
    options.add_argument("--disable-browser-side-navigation")
    
    # --- 自動化フラグの隠蔽 ---
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    # --- ユーザーデータディレクトリ（競合回避） ---
    # ★修正: 実行ごとにランダムなディレクトリを作成し、権限エラーとロック競合を防ぐ
    user_data_dir = f"/tmp/chrome_data_{uuid.uuid4()}"
    options.add_argument(f"--user-data-dir={user_data_dir}")

    # --- バイナリ場所 ---
    chrome_binary_path = os.environ.get("CHROME_BINARY_LOCATION")
    if chrome_binary_path:
        if os.path.exists(chrome_binary_path):
            options.binary_location = chrome_binary_path
        else:
            # 見つからない場合は標準パスを探す
            default_path = "/usr/bin/google-chrome"
            if os.path.exists(default_path):
                options.binary_location = default_path

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        # navigator.webdriver フラグを消す
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return driver
    except Exception as e:
        print(f"CRITICAL ERROR in create_driver: {e}")
        # 失敗時にゴミを残さない
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

    # body が出るまで待機
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(1) # 少し待つ
    except Exception:
        pass

    # ---- タイトル ----
    try:
        # h1が見つからない場合もあるので待機時間を短めに
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
        
        # ★デバッグ用: 開いたページのタイトルを表示
        print(f"DEBUG: Page Title = {driver.title}")
        
        wait = WebDriverWait(driver, 15) # 待機時間を少し延長
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            print("DEBUG: Timeout waiting for body")

        # スクロール
        for i in range(max_scroll):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3) # 待ち時間を2→3秒へ延長

        # リンク取得
        # セレクタを少し広めに取る（構造変化対策）
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")
        
        # もし見つからなかった場合、別のセレクタも試す（保険）
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
            if data["title"]: # タイトルが取れていれば成功とみなす
                 print(f"DEBUG: Success -> {data['title']}")
            else:
                 print("DEBUG: Failed to get title (Bot block?)")
            
            items.append(data)
            time.sleep(3)  # アクセス間隔を少し広げる

        return items

    except Exception as e:
        print(f"CRITICAL ERROR during scraping: {e}")
        # エラーの詳細（スタックトレース）を表示
        import traceback
        traceback.print_exc()
        return []
    finally:
        if driver:
            try:
                # 終了時にディレクトリ掃除をするために取得しておく
                user_data_dir = None
                for arg in driver.capabilities.get('chrome', {}).get('userDataDir', ''):
                     # capsから取れればベストだが、オプションから取るのが確実
                     pass
                driver.quit()
            except:
                pass
