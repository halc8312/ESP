import logging
import asyncio
from playwright.async_api import async_playwright
from scrapling import StealthyFetcher
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


logger = logging.getLogger("mercari")

def _get_or_create_event_loop():
    """
    現在のスレッドのイベントループを取得、または新規作成する。
    Flask + Gunicorn gthread 環境での asyncio 使用に対応。
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def _extract_price_from_text(text: str):
    if not text:
        return None

    for pattern in (r"[¥￥]\s*([\d,]+)", r"([\d,]+)\s*円"):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            continue

    return None


def _normalize_mercari_shops_title(text: str) -> str:
    if not text:
        return ""

    normalized = text.strip()
    normalized = re.sub(r"\s*[-|｜]\s*メルカリ(?:\s*Shops)?\s*$", "", normalized).strip()
    normalized = re.sub(r"\s*[-|｜]\s*Mercari(?:\s*Shops)?\s*$", "", normalized).strip()

    if normalized in {"メルカリ", "Mercari"}:
        return ""
    return normalized


def _extract_mercari_shops_title_from_body(body_text: str) -> str:
    if not body_text:
        return ""

    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    skip_lines = {"コンテンツにスキップ", "ログイン", "会員登録", "出品", "日本語", "メルカリShops", "質問する"}

    for index, line in enumerate(lines):
        if line in skip_lines:
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", line):
            continue
        if _extract_price_from_text(line) is not None or line == "¥":
            continue

        nearby_lines = lines[index + 1:index + 4]
        if any(candidate == "¥" or _extract_price_from_text(candidate) is not None for candidate in nearby_lines):
            return line

    return ""


def _infer_mercari_shops_status(body_text: str) -> str:
    if not body_text:
        return "on_sale"

    purchase_markers = ("購入手続きへ", "カートに入れる", "今すぐ購入")
    sold_markers = ("この商品は売り切れです", "在庫なし", "現在在庫がありません")

    if any(marker in body_text for marker in purchase_markers):
        return "on_sale"
    if any(marker in body_text for marker in sold_markers):
        return "sold"
    if "売り切れ" in body_text and "残り" not in body_text:
        return "sold"
    return "on_sale"


async def _extract_first_non_empty_text_async(page, selectors: list) -> str:
    for selector in selectors:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue

        for element in elements:
            try:
                candidate = (await element.inner_text()).strip()
            except Exception:
                continue
            if candidate:
                return candidate

    return ""



async def _extract_shops_variants_async(page, label_texts: list) -> list:
    """
    メルカリShopsのバリエーションを Playwright で取得。
    """
    found_options = []
    
    for label_text in label_texts:
        # ラベルテキストを含む要素を XPath で検索
        labels = await page.query_selector_all(
            f"xpath=//*[contains(text(), '{label_text}')]"
        )
        
        for label in labels:
            try:
                tag_name = await label.evaluate("el => el.tagName.toLowerCase()")
                if tag_name in ['script', 'style']:
                    continue
                
                # 親要素の nextElementSibling（コンテナ）を取得
                container = await label.evaluate_handle(
                    "el => el.parentElement && el.parentElement.nextElementSibling"
                )
                
                # Check for truthful element handle (not None and not evaluating to null)
                is_valid = await container.evaluate("el => el !== null")
                if not is_valid:
                    continue
                
                # コンテナの直下の子要素を取得
                children = await container.query_selector_all(":scope > *")
                
                if children:
                    options = []
                    for child in children:
                        raw_text = await child.inner_text()
                        raw_text = raw_text.strip()
                        if not raw_text:
                            continue
                        
                        # 1行目のみ取得
                        val = raw_text.split('\n')[0].strip()
                        
                        # 価格・在庫情報を削除（正規表現クリーニング）
                        val = re.sub(r'[¥￥]\s*[\d,]+', '', val)
                        val = re.sub(r'[\d,]+\s*円', '', val)
                        val = re.sub(r'残り\d+点', '', val)
                        val = re.sub(r'売り切れ|在庫なし', '', val)
                        val = val.strip()
                        
                        # 不要なボタンを除外
                        if val and val not in ["いいね", "シェア", "もっと見る"]:
                            if val not in options:
                                options.append(val)
                    
                    if options:
                        found_options = options
                        break
                        
            except Exception:
                continue
        
        if found_options:
            break
    
    return found_options


async def _scrape_shops_product_async(url: str) -> dict:
    """メルカリShops商品ページを Playwright で取得"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            try:
                await page.wait_for_selector("h1, [data-testid='product-price']", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)  # Shopsはロードが遅いことがあるため待機
            body_text = await page.evaluate("document.body.innerText")
            
            # ---- タイトル ----
            title_selectors = get_selectors('mercari', 'shops', 'title') or ["[data-testid='product-name']", "h1"]
            title = await _extract_first_non_empty_text_async(page, title_selectors)
            if not title:
                title = _normalize_mercari_shops_title(await page.title())
            if not title:
                title = _extract_mercari_shops_title_from_body(body_text)

            # ---- 価格 ----
            price = None
            price_selectors = get_selectors('mercari', 'shops', 'price') or ["[data-testid='product-price']"]
            for selector in price_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                except Exception:
                    continue
                if elements:
                    price_text = await elements[0].inner_text()
                    price = _extract_price_from_text(price_text)
                    if price is not None:
                        break
                        
            # 予備の価格取得ロジック
            if price is None:
                price = _extract_price_from_text(body_text)

            # ---- 説明文 ----
            desc_selectors = get_selectors('mercari', 'shops', 'description') or ["[data-testid='product-description']"]
            description = await _extract_first_non_empty_text_async(page, desc_selectors)

            # 説明文フォールバック: meta description
            if not description:
                meta = await page.query_selector("meta[name='description']")
                if meta:
                    description = await meta.get_attribute("content") or ""

            # 説明文フォールバック: body text から抽出
            if not description:
                body_text = await page.evaluate("document.body.innerText")
                if "商品の説明" in body_text:
                    after = body_text.split("商品の説明", 1)[1]
                    end_pos = len(after)
                    for marker in ["商品の情報", "ショップ情報", "おすすめ商品", "レビュー"]:
                        idx = after.find(marker)
                        if idx != -1 and idx < end_pos:
                            end_pos = idx
                    description = after[:end_pos].strip()[:500]

            # ---- 画像 ----
            image_urls = []
            image_selectors = get_selectors('mercari', 'shops', 'images') or ["img[src*='mercari'][src*='static']"]
            for selector in image_selectors:
                try:
                    imgs = await page.query_selector_all(selector)
                except Exception:
                    continue
                for img in imgs:
                    src = await img.get_attribute("src")
                    if src and src not in image_urls:
                        image_urls.append(src)

            # ---- バリエーション（簡易取得） ----
            variants = []
            item_data_update = {}
            
            colors = await _extract_shops_variants_async(page, ['カラー', 'Color'])
            types = await _extract_shops_variants_async(page, ['種類', 'サイズ', 'Size'])
            logger.debug("Mercari Shops variants: colors=%s types=%s", colors, types)

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

            # ---- ステータス ----
            status = _infer_mercari_shops_status(body_text)

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
            
        finally:
            await browser.close()


def scrape_shops_product(url: str, driver=None) -> dict:
    """メルカリShops商品ページ用スクレイピング（同期ラッパー）"""
    loop = _get_or_create_event_loop()
    return loop.run_until_complete(_scrape_shops_product_async(url))



def scrape_item_detail(url: str, driver=None):
    """1つの商品ページから詳細情報を取得して dict で返す"""
    
    # Shops URL判定
    if "/shops/product/" in url:
        return scrape_shops_product(url)

    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=False)
    except Exception as e:
        print(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error", 
            "description": "", "image_urls": [], "variants": []
        }

    # ---- タイトル ----
    try:
        title_nodes = page.css("h1")
        title_el = title_nodes[0] if title_nodes else None
        title = title_el.text.strip() if title_el else ""
    except Exception:
        title = ""

    # ---- ページ全体のテキスト ----
    try:
        body_text = " ".join([el.text for el in page.css("body *") if el.text])
    except Exception:
        body_text = ""

    # ---- 価格 ----
    price = None
    price_selectors = get_selectors('mercari', 'general', 'price') or ["[data-testid='price']"]
    try:
        # メルカリのクラス名は頻繁に変わるため、data-testidがあれば優先
        for selector in price_selectors:
            price_nodes = page.css(selector)
            if price_nodes:
                price_text = price_nodes[0].text or ""
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
             try:
                 for btn in page.css("button"):
                     if "売り切れ" in (btn.text or ""):
                         status = "sold"
                         break
             except Exception:
                 status = "sold"  # Fallback
        
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
        for selector in image_selectors:
            img_elements = page.css(selector)
            for img in img_elements:
                src = img.attrib.get("src")
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
             "button[aria-haspopup='listbox']",
             "div[role='radiogroup'] div[role='radio']",
             "div[data-testid='product-variant-selector'] button"
        ]
        
        found_elements = []
        for sel in selectors:
            found_elements = page.css(sel)
            if found_elements and len(found_elements) > 1:
                break
                
        seen_opts = set()
        for el in found_elements:
            text_val = el.text.strip() if el.text else ""
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


async def _scrape_search_async(
    search_url: str,
    max_items: int,
    max_scroll: int,
) -> list[str]:
    """ページスクロールしながら商品リンクを収集する非同期関数"""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
            ]
        )
        
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            # Bot検知対策
            extra_http_headers={
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            }
        )
        page = await context.new_page()
        
        # Bot 検知対策: webdriver フラグを隠す
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        
        try:
            print(f"DEBUG: Navigating to {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000) # 初期ロード待機
            
            item_urls = set()
            
            for scroll_count in range(max_scroll * 2):
                # 現在表示されているリンクを収集（/item/ あるいはテストデータ等）
                links = await page.query_selector_all("a[href*='/item/']")
                if not links:
                    links = await page.query_selector_all("li[data-testid='item-cell'] a")
                    
                for link in links:
                    href = await link.get_attribute("href")
                    if href and "/item/" in href:
                        # 絶対URLに変換
                        if href.startswith("/"):
                            href = f"https://jp.mercari.com{href}"
                        item_urls.add(href)
                
                if len(item_urls) >= max_items * 2:
                    break
                
                # ページを下にスクロール
                prev_count = len(item_urls)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)  # 2秒待機（新アイテム読み込み）
                
                # スクロール後に新しいリンクが増えていない場合は終了
                links_after = await page.query_selector_all("a[href*='/item/']")
                if not links_after:
                     links_after = await page.query_selector_all("li[data-testid='item-cell'] a")
                if len(links_after) <= len(links):
                    # 成長していなければストップ
                    break
            
            print(f"DEBUG: Found {len(item_urls)} valid item URLs.")
            return list(item_urls)[:max_items * 2]  # 最大 max_items * 2 件
            
        finally:
            await browser.close()


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
):
    """
    メルカリ検索URLから複数商品をスクレイピングして list[dict] を返す。
    Playwright を直接使用してページスクロール取得を実現。
    """
    loop = _get_or_create_event_loop()
    
    try:
        item_urls = loop.run_until_complete(
            _scrape_search_async(search_url, max_items, max_scroll)
        )
    except Exception as e:
        logging.error(f"Search scrape failed: {e}")
        return []
    
    filtered_items = []
    for url in item_urls:
        if len(filtered_items) >= max_items:
            break
            
        print(f"DEBUG: Scraping item {url}")
        try:
            data = scrape_item_detail(url)
            if data and data.get("title") and data.get("status") != "error":
                 print(f"DEBUG: Success -> {data['title']}")
                 filtered_items.append(data)
            else:
                 print("DEBUG: Failed to get valid data (empty title or error)")
        except Exception as e:
            print(f"DEBUG: Error scraping {url}: {e}")
            
        time.sleep(1) 
    
    return filtered_items


def scrape_single_item(url: str, headless: bool = True):
    """
    指定された商品URLを1件だけスクレイピングして list[dict] を返す。
    save_scraped_items_to_db にそのまま渡せるようにリストに包んでいる。
    """
    metrics = get_metrics()
    metrics.start('mercari', 'single')
    try:
        logger.debug("Starting scrape_single_item for %s", url)
        
        data = scrape_item_detail(url)
        log_scrape_result('mercari', url, data)
        
        if data.get("title"):
            logger.debug("Mercari scrape success: %s", data["title"])
        else:
            logger.debug("Mercari scrape failed to get title for %s", url)

        metrics.finish()
        return [data]

    except Exception as e:
        print(f"CRITICAL ERROR during single scraping: {e}")
        import traceback
        traceback.print_exc()
        metrics.record_attempt(False, url, str(e))
        metrics.finish()
        return []
