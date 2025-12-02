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


def create_driver(headless: bool = True):
    """Chrome WebDriver を生成"""
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    # ★追加: これがないとDocker内でクラッシュすることがある
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--window-size=1280,1024")
    
    # RenderでインストールしたChromeの場所を指定
    chrome_binary_path = os.environ.get("CHROME_BINARY_LOCATION")
    if chrome_binary_path:
        options.binary_location = chrome_binary_path

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    return driver


def scrape_item_detail(driver, url: str):
    """1つの商品ページから詳細情報を取得して dict で返す"""
    driver.get(url)
    wait = WebDriverWait(driver, 10)

    # body が出るまで待機
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception:
        pass

    # ---- タイトル ----
    try:
        title_el = wait.until(EC.presence_of_element_located((By.TAG_NAME, "h1")))
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

    # 1) data-testid など属性での取得（あれば）
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

    # 2) ダメな場合は body テキストから拾う
    if price is None and body_text:
        m = re.search(r"[¥￥]\s*([\d,]+)", body_text)
        if not m:
            m = re.search(r"([\d,]+)\s*円", body_text)
        if m:
            try:
                price = int(m.group(1).replace(",", ""))
            except ValueError:
                price = None

    # ---- ステータス（販売中 / 売り切れ）----
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
    search_url は外から作る（Flask 側でキーワード・価格条件などを付与）
    """
    driver = create_driver(headless=headless)
    items = []

    try:
        driver.get(search_url)
        wait = WebDriverWait(driver, 10)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            pass

        # スクロールして商品を読み込む
        for _ in range(max_scroll):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")
        print("検索ページで見つかったリンク数:", len(links))

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

        print("詳細ページを見に行く件数:", len(item_urls))

        for url in item_urls:
            data = scrape_item_detail(driver, url)
            items.append(data)
            time.sleep(1.5)  # サイト負荷対策

        return items

    finally:
        driver.quit()
