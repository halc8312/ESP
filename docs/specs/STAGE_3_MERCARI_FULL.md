# Stage 3: メルカリ全体 Playwright 移行仕様書

## 読むべきドキュメント

1. [CURRENT_ARCHITECTURE.md](./CURRENT_ARCHITECTURE.md) — 現在のコードベース構造
2. [STAGE_0_QUEUE_SYSTEM.md](./STAGE_0_QUEUE_SYSTEM.md) — キューシステム仕様
3. [STAGE_1_RAKUMA_PLAYWRIGHT.md](./STAGE_1_RAKUMA_PLAYWRIGHT.md) — ラクマ移行仕様
4. [STAGE_2_MERCARI_PATROL.md](./STAGE_2_MERCARI_PATROL.md) — メルカリパトロール移行仕様
5. **`docs/specs/STAGE_1_RESULTS.md`** — Render 互換性検証結果 ← **必読**
6. **`docs/specs/STAGE_2_RESULTS.md`** — メルカリパトロール移行結果 ← **必読**

---

## 前提条件

- **Stage 2 完了済み**: `MercariPatrol` が Playwright で動作している
- **`docs/specs/STAGE_2_RESULTS.md` が存在する**: Bot 検知挙動の観察事項が記載されている

> ⚠️ **STAGE_2_RESULTS.md が存在しない場合は Stage 2 が未完了です。先に Stage 2 を実施してください。**

---

## 目標

`mercari_db.py`（~608行）の全機能を Selenium から Playwright に移行する。

移行対象の関数：

| 関数名                    | 行数目安 | 移行の複雑さ |
|---------------------------|----------|--------------|
| `create_driver()`         | ~48行    | 削除（不要）  |
| `_get_chrome_version()`   | ~30行    | 削除（不要）  |
| `scrape_shops_product()`  | ~230行   | **高**（バリエーション取得が複雑） |
| `scrape_item_detail()`    | ~160行   | **中**（基本的な変換） |
| `scrape_search_result()`  | ~95行    | **高**（スクロール処理が必要）|
| `scrape_single_item()`    | ~40行    | 低           |

---

## 三大技術的課題と解決策

### 課題 1: 検索スクロール読み込み

**現在の Selenium 実装** (`scrape_search_result()`, ~540〜562行):

```python
# 現在
while len(links) < max_items * 2 and scroll_attempts < max_scroll * 2:
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)
    
    new_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")
    # ...
```

**問題**: `StealthyFetcher.fetch()` はシングルショットで、インタラクティブなスクロールができない。

**解決策**: Playwright のブラウザコンテキストを**直接**使用する（StealthyFetcher は使わない）。

**疑似コード（Playwright 直接 API）**:

```python
import asyncio
from playwright.async_api import async_playwright


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
                "--disable-dev-shm-usage",  # Render の /dev/shm が小さい場合
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
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            
            item_urls = set()
            
            for scroll_count in range(max_scroll * 2):
                # 現在表示されているリンクを収集
                links = await page.query_selector_all("a[href*='/item/']")
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
                if len(links_after) == len(links):
                    break
            
            return list(item_urls)[:max_items * 2]  # 最大 max_items * 2 件
            
        finally:
            await browser.close()


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


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
) -> list[dict]:
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
    
    items = []
    for url in item_urls:
        if len(items) >= max_items:
            break
        try:
            data = scrape_single_item(url, headless=headless)
            items.extend(data)
        except Exception as e:
            logging.warning(f"Failed to scrape {url}: {e}")
    
    return items
```

> **asyncio と Flask の互換性に関する注意**:
> Gunicorn + gthread ワーカーでは、スレッドごとにイベントループが別々になる。
> `asyncio.run()` はメインスレッドのみで動作する場合があるため、
> `asyncio.new_event_loop()` + `loop.run_until_complete()` のパターンを使用すること。
>
> もし `RuntimeError: This event loop is already running` が発生する場合は、
> `nest_asyncio` パッケージを `pip install nest_asyncio` してから `nest_asyncio.apply()` を使う。

---

### 課題 2: メルカリShops バリエーション取得

**現在の Selenium 実装** (`scrape_shops_product()`, ~244行):

```python
# 現在の複雑な DOM 操作
container = driver.execute_script(
    "return arguments[0].nextElementSibling", parent
)
children = container.find_elements(By.XPATH, "./*")
```

**問題**: `execute_script("return arguments[0].nextElementSibling", el)` は
Selenium の JavaScript 実行機能を使っており、`StealthyFetcher` では使えない。

**解決策**: Playwright 直接 API で同等の操作を実現する。

**疑似コード**:

```python
async def _extract_shops_variants_async(page, label_texts: list) -> list:
    """
    メルカリShopsのバリエーションを Playwright で取得。
    
    DOM 構造:
      <span>カラー</span>  ← ラベル
      <div>               ← 親要素（label.find_element(By.XPATH, "..")）
        <div>             ← コンテナ（execute_script で取得していた nextElementSibling）
          <div>赤</div>   ← オプション1
          <div>青</div>   ← オプション2
        </div>
      </div>
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
                # Selenium の execute_script("return arguments[0].nextElementSibling", parent) の置き換え
                container = await label.evaluate_handle(
                    "el => el.parentElement && el.parentElement.nextElementSibling"
                )
                
                if not container:
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
                        import re
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
```

---

### 課題 3: 遅延読み込み画像

**現在の Selenium 実装** (`scrape_item_detail()`, ~450行):

```python
# 現在
time.sleep(1)  # 遅延読み込みを待つ
for img in img_elements:
    src = img.get_attribute("src")
    if src and src not in image_urls:
        image_urls.append(src)
```

**問題**: `time.sleep(1)` は不確実で、遅延読み込みが完了していない場合がある。

**解決策**: Playwright で CDN 画像の出現を待機する。

```python
# Playwright での解決策
async def _wait_for_images_and_extract(page):
    """CDN 画像のロードを待ってから URL を取得"""
    
    # メルカリの CDN 画像が読み込まれるまで待機（最大5秒）
    try:
        await page.wait_for_selector(
            "img[src*='mercdn.net']",
            timeout=5000,
            state="attached"  # DOM に存在すれば OK（visible でなくても）
        )
    except Exception:
        # タイムアウトしても続行（一部の画像は取れない可能性がある）
        pass
    
    # または networkidle を待機
    # await page.wait_for_load_state("networkidle", timeout=5000)
    
    imgs = await page.query_selector_all(
        "img[src*='static.mercdn.net'][src*='/item/'][src*='/photos/']"
    )
    
    image_urls = []
    for img in imgs:
        src = await img.get_attribute("src")
        if src and src not in image_urls:
            image_urls.append(src)
    
    return image_urls
```

---

## `mercari_db.py` 全体の移行方針

### 削除する関数・インポート

```python
# 削除するインポート
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# 削除する関数
def _get_chrome_version(): ...  # Chrome バージョン検出（不要）
def create_driver(headless: bool = True): ...  # Selenium ドライバー生成（不要）
```

### 追加するインポート

```python
import asyncio
from playwright.async_api import async_playwright
from scrapling import StealthyFetcher  # シングルショット取得用
```

### `scrape_item_detail()` の書き換え方針

通常商品ページはシングルショットで取得できるため `StealthyFetcher.fetch()` を使用：

```python
def scrape_item_detail(url: str) -> dict:
    """
    1つのメルカリ商品ページから詳細情報を取得して dict で返す。
    
    Note: driver 引数は廃止。後方互換のためにシグネチャを保持する場合：
    def scrape_item_detail(url: str, driver=None) -> dict:
    """
    # Shops URL の場合は Shops 専用関数へ
    if "/shops/product/" in url:
        return scrape_shops_product(url)
    
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
    
    # タイトル
    title_el = page.css_first("h1")
    title = title_el.text.strip() if title_el else ""
    
    body_text = page.get_text()
    
    # 価格
    price = None
    price_selectors = ["[data-testid='price']"]
    for sel in price_selectors:
        el = page.css_first(sel)
        if el:
            m = re.search(r"[¥￥]\s*([\d,]+)", el.text)
            if m:
                price = int(m.group(1).replace(",", ""))
                break
    
    # ステータス
    status = "unknown"
    if "売り切れ" in body_text or "Sold" in body_text:
        status = "sold"
    elif "購入手続きへ" in body_text or "Buy this item" in body_text:
        status = "on_sale"
    
    # 説明文
    description = ""
    if "商品の説明" in body_text:
        after = body_text.split("商品の説明", 1)[1]
        end_pos = len(after)
        for marker in ["商品の情報", "商品情報", "出品者", "コメント"]:
            idx = after.find(marker)
            if idx != -1 and idx < end_pos:
                end_pos = idx
        description = after[:end_pos].strip()
    
    # 画像（遅延読み込みは network_idle=True でカバー）
    image_urls = []
    img_selector = "img[src*='static.mercdn.net'][src*='/item/'][src*='/photos/']"
    for img in page.css(img_selector):
        src = img.attrib.get("src")
        if src and src not in image_urls:
            image_urls.append(src)
    
    # バリエーション（シンプルな取得）
    variants = []
    # ... （省略: Selenium 版と同等のロジックを Scrapling CSS セレクタで実装）
    
    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": variants,
    }
```

### `scrape_shops_product()` の書き換え方針

バリエーション取得が複雑なため、Playwright 直接 API（`async_playwright`）を使用する：

```python
async def _scrape_shops_product_async(url: str) -> dict:
    """メルカリShops商品ページを Playwright で取得"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context()
        page = await context.new_page()
        
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(2000)  # Shops はロードが遅い
        
        # タイトル、価格、説明文、画像を取得
        # ...
        
        # バリエーション取得（課題2の疑似コードを使用）
        colors = await _extract_shops_variants_async(page, ['カラー', 'Color'])
        types = await _extract_shops_variants_async(page, ['種類', 'サイズ', 'Size'])        
        await browser.close()
        return {...}


def scrape_shops_product(url: str) -> dict:
    """同期ラッパー"""
    loop = _get_or_create_event_loop()
    return loop.run_until_complete(_scrape_shops_product_async(url))
```

---

## 変更するファイル

### 1. `mercari_db.py`（全面書き換え、~608行）

主な変更:
- Selenium インポートを全て削除
- `create_driver()`, `_get_chrome_version()` を削除
- 全関数を Playwright/Scrapling で書き直し
- 関数シグネチャの変更:
  - `scrape_shops_product(driver, url)` → `scrape_shops_product(url)`
  - `scrape_item_detail(driver, url)` → `scrape_item_detail(url)`
  - `scrape_search_result()`, `scrape_single_item()` は外部 API 変更なし（引数の意味は変わらない）

### 2. `services/scrape_queue.py`（修正）

メルカリを `http_executor` へ移行：

```python
# Stage 3 完了後
BROWSER_SITES = frozenset()  # 全サイトが Playwright 対応 → browser_executor 不要
```

または Playwright をより多くの並行数で動かす場合：

```python
# Playwright は Selenium より軽量なため、並行数を増やせる
BROWSER_SITES = frozenset()
# http_executor の max_workers を調整（全サイトが HTTP/Playwright）
```

### 3. `tests/test_scraping_logic.py`（修正）

既存テストは Selenium モックを使用している。Playwright/Scrapling のモックに変更：

```python
# 現在のモック（Selenium）
@pytest.fixture
def mock_driver():
    driver = MagicMock()
    driver.find_elements.return_value = [...]
    return driver

# 移行後のモック（Scrapling）
@pytest.fixture
def mock_page():
    page = MagicMock()
    page.css.return_value = [...]
    page.css_first.return_value = MagicMock(text="テストタイトル")
    page.get_text.return_value = "¥1,000 販売中"
    return page
```

---

## asyncio 設計の注意事項

### イベントループの管理

Flask アプリが Gunicorn + gthread で動作している場合:

```
スレッド1（Gunicornワーカー1）
  ├── リクエスト処理
  └── scrape_search_result() 呼び出し
        └── asyncio.new_event_loop() で新規ループ作成
              └── loop.run_until_complete(_scrape_search_async())

スレッド2（Gunicornワーカー2）  
  ├── 別のリクエスト処理
  └── 同様に独立したイベントループ
```

各スレッドが独立したイベントループを持つため、スレッド間の競合はない。
ただし、`asyncio.get_event_loop()` はスレッドローカルなイベントループを返すため、
スレッドで新しくイベントループを作成する場合は `asyncio.set_event_loop(loop)` も必要。

### Playwright ブラウザのライフサイクル

**推奨**: 1スクレイピングタスクごとにブラウザを作成・廃棄する（メモリリーク防止）:

```python
async with async_playwright() as p:
    browser = await p.chromium.launch(...)
    try:
        # スクレイピング処理
        result = await _do_scraping(browser, url)
    finally:
        await browser.close()
    return result
```

**非推奨**: ブラウザインスタンスを長時間保持（メモリリーク・クラッシュのリスク）

---

## テスト要件

### `tests/test_mercari_playwright.py` 新規作成

```python
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


def test_scrape_item_detail_structure():
    """scrape_item_detail が正しい構造を返すことを確認"""
    mock_page = MagicMock()
    mock_page.css_first.side_effect = lambda sel: MagicMock(
        text="テスト商品" if "h1" in sel else "¥2,000",
        attrib={}
    )
    mock_page.css.return_value = []
    mock_page.get_text.return_value = "テスト商品 ¥2,000 購入手続きへ"
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from mercari_db import scrape_item_detail
        result = scrape_item_detail("https://jp.mercari.com/item/test123")
    
    assert "title" in result
    assert "price" in result
    assert "status" in result
    assert "image_urls" in result
    assert isinstance(result["variants"], list)


def test_scrape_single_item_returns_list():
    """scrape_single_item がリストを返すことを確認"""
    mock_page = MagicMock()
    mock_page.css_first.return_value = MagicMock(text="テスト商品", attrib={})
    mock_page.css.return_value = []
    mock_page.get_text.return_value = "テスト商品 ¥1,500"
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from mercari_db import scrape_single_item
        items = scrape_single_item("https://jp.mercari.com/item/test123")
    
    assert isinstance(items, list)


def test_create_driver_removed():
    """create_driver が削除されていることを確認（Stage 4 の前提）"""
    import importlib
    import mercari_db
    importlib.reload(mercari_db)
    
    assert not hasattr(mercari_db, 'create_driver'), \
        "create_driver should be removed in Stage 3"
```

### 既存テストの更新

`tests/test_scraping_logic.py` の Selenium モックを Scrapling/Playwright モックに置き換える。
既存のテストロジック（スクレイピング結果の検証）は保持したまま、モック部分のみ変更すること。

---

## 次の Agent への引き継ぎ（Stage 4 の担当者へ）

### Stage 3 完了の確認事項

Stage 4 を開始する前に、以下が完了していることを確認：

1. **`create_driver()` が `mercari_db.py` から削除されている**
2. **`services/scrape_queue.py` の `BROWSER_SITES` が空集合になっている**
3. **`tests/test_scraping_logic.py` が全て通過している**
4. **実際のメルカリ URL でスクレイピングが成功している**

### Stage 4 での Selenium 削除対象

Stage 4 では以下を削除する：

```
Dockerfile:
  - Google Chrome インストールブロック全体
  - Chrome 関連の apt パッケージ

requirements.txt:
  - selenium
  - webdriver-manager
  - undetected-chromedriver

コード:
  - mercari_db.py: create_driver(), _get_chrome_version()（Stage 3 で削除済みのはず）
  - surugaya_db.py: _fetch_soup_with_selenium() の Selenium 依存部分
  - services/patrol/yahoo_patrol.py: _fetch_with_selenium() メソッド
  
削除するデバッグスクリプト:
  - debug_scrape.py
  - debug_children.py
  - debug_variant_json.py
```

### `docs/specs/STAGE_3_RESULTS.md` の作成

Stage 3 完了後、以下を記録する：

```markdown
# Stage 3 実施結果

## メモリ使用量の実測値
- 1検索操作（スクロール × 3）: XXX MB
- 1商品詳細取得: XXX MB

## asyncio + Flask の相性
- 問題があったか？どう解決したか？

## メルカリShopsのバリエーション取得
- 成功したか？
- 特別な対処が必要だったか？

## Bot 検知への対処
- CAPTCHA が発生したか？
- どの設定で回避できたか？
```
