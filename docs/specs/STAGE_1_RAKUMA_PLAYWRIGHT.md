# Stage 1: ラクマ Playwright 移行仕様書

## 読むべきドキュメント

1. [CURRENT_ARCHITECTURE.md](./CURRENT_ARCHITECTURE.md) — 現在のコードベース構造
2. [STAGE_0_QUEUE_SYSTEM.md](./STAGE_0_QUEUE_SYSTEM.md) — Stage 0 のキューシステム仕様

---

## 前提条件

- **Stage 0 完了済み**: `services/scrape_queue.py` が実装され、スクレイピングリクエストがキュー経由で処理されている

---

## 目標

1. **ラクマのスクレイピングを Selenium から Playwright（Scrapling StealthyFetcher）に移行する**
2. **Render 本番環境での Playwright の動作互換性を検証する**（後続 Stage のための重要ゲート）

> ⚠️ **この Stage は「Render 互換性の検証」が最大の目的です。**
> Playwright が Render Standard プランで正常に動作することを確認してから、
> より複雑な Mercari の移行（Stage 2〜3）に進んでください。

---

## 現在のコード分析

### `rakuma_db.py`（~184行）

全関数が Selenium に依存している：

```python
# 現在のインポート
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from mercari_db import create_driver  # ← Selenium の Chrome ドライバー生成
```

**主要な Selenium 使用箇所**:

| 行番号 | 使用箇所                                 | 用途                               |
|--------|------------------------------------------|------------------------------------|
| 11     | `from mercari_db import create_driver`   | Chrome WebDriver の生成            |
| 21     | `driver.get(url)`                        | URL へのナビゲーション             |
| 29     | `WebDriverWait(driver, 10).until(...)`   | ページロード待機                   |
| 32     | `time.sleep(2)`                          | SPA ロード待機                     |
| 41-46  | `driver.find_elements(By.CSS_SELECTOR)`  | 要素の検索                         |
| 97-101 | `img.get_attribute("src"/"data-lazy")`   | 遅延読み込み画像の取得             |
| 209    | `driver.execute_script("window.scrollTo...")` | ページスクロール                |

### `services/patrol/rakuma_patrol.py`（~109行）

同様に Selenium に依存：

```python
from mercari_db import create_driver  # ← Selenium
```

---

## Selenium → Playwright API 対応表

| Selenium（現在）                                        | Playwright 非同期 API（移行後）                              |
|---------------------------------------------------------|--------------------------------------------------------------|
| `driver.get(url)`                                       | `await page.goto(url)`                                       |
| `driver.find_elements(By.CSS_SELECTOR, sel)`            | `await page.query_selector_all(sel)`                         |
| `element.text`                                          | `await element.text_content()`                               |
| `element.get_attribute("src")`                          | `await element.get_attribute("src")`                         |
| `WebDriverWait(driver, N).until(EC.presence_of(...))`  | `await page.wait_for_selector(sel, timeout=N*1000)`          |
| `time.sleep(N)`                                         | `await page.wait_for_timeout(N*1000)`                        |
| `driver.execute_script("window.scrollTo(0, ...)")`     | `await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")`|
| `body.text`                                             | `await page.inner_text("body")`                              |
| `driver.title`                                          | `page.title()`（または `await page.title()`）                |

### Scrapling StealthyFetcher 版（シングルショット）

`rakuma_db.py` の `scrape_item_detail` は**1ページを取得して解析するだけ**なので、
Scrapling の `StealthyFetcher.fetch()` が最もシンプルに使える。

```python
from scrapling import StealthyFetcher

def scrape_item_detail(url: str) -> dict:
    """
    Scrapling StealthyFetcher を使用してラクマ商品ページを取得・解析する。
    driver 引数は廃止（後方互換のため残す場合は無視）。
    """
    page = StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,  # JS ロード待機
    )
    
    # タイトル取得
    title = ""
    for selector in ["h1.item__name", "h1"]:
        el = page.css_first(selector)
        if el:
            title = el.text.strip()
            break
    
    # 価格取得
    price = None
    for selector in ["span.item__price", ".item__price", "[class*='price']"]:
        el = page.css_first(selector)
        if el:
            m = re.search(r"[¥￥]\s*([\d,]+)", el.text)
            if m:
                price = int(m.group(1).replace(",", ""))
                break
    
    # ... (説明文、画像、ステータス)
    
    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": [],
    }
```

### 検索結果（スクロール）の場合

`scrape_search_result` はページスクロールが必要なため、`StealthyFetcher.fetch()` の
シングルショットでは対応できない。**Playwright のブラウザコンテキストを直接使用する**必要がある。

```python
import asyncio
from playwright.async_api import async_playwright

async def _scrape_search_async(search_url: str, max_items: int, max_scroll: int):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 ...",
        )
        page = await context.new_page()
        
        await page.goto(search_url, wait_until="networkidle")
        
        links = []
        for _ in range(max_scroll):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            
            new_links = await page.query_selector_all("a.link_search_image, a.link_search_title")
            # ... リンク収集
        
        await browser.close()
        return links


def scrape_search_result(search_url, max_items=5, max_scroll=3, headless=True):
    """同期ラッパー"""
    return asyncio.run(_scrape_search_async(search_url, max_items, max_scroll))
```

> **注意**: `asyncio.run()` はスレッド内から呼び出せない。
> Flask が Gunicorn + gthread で動作している場合、バックグラウンドスレッドから
> `asyncio.run()` を呼ぶと `RuntimeError: This event loop is already running` が発生する場合がある。
> この場合は、以下の `_get_or_create_event_loop()` パターン（Stage 3 でも使用）を使用すること：
>
> ```python
> def _get_or_create_event_loop():
>     """スレッドセーフなイベントループ取得"""
>     try:
>         loop = asyncio.get_event_loop()
>         if loop.is_closed():
>             loop = asyncio.new_event_loop()
>             asyncio.set_event_loop(loop)
>         return loop
>     except RuntimeError:
>         loop = asyncio.new_event_loop()
>         asyncio.set_event_loop(loop)
>         return loop
>
> def scrape_search_result(search_url, max_items=5, max_scroll=3, headless=True):
>     """同期ラッパー"""
>     loop = _get_or_create_event_loop()
>     return loop.run_until_complete(_scrape_search_async(search_url, max_items, max_scroll))
> ```

---

## 変更するファイル

### 1. `rakuma_db.py`（全面書き換え）

**変更の要点**:
- `from mercari_db import create_driver` を削除
- `from selenium.webdriver.common.by import By` 等を削除
- `driver` 引数を受け取る関数シグネチャを変更
  - `scrape_item_detail(driver, url)` → `scrape_item_detail(url)`
  - ただし後方互換のため `driver=None` を保持しても良い（無視される）
- `StealthyFetcher.fetch()` または Playwright 直接 API を使用

**シグネチャ変更の影響を確認**:
```bash
grep -n "scrape_item_detail" /home/runner/work/ESP/ESP/rakuma_db.py
grep -rn "rakuma_db.scrape_item_detail" /home/runner/work/ESP/ESP/
```

現在、`scrape_item_detail(driver, url)` は `scrape_single_item` と `scrape_search_result` 内から
呼ばれており、両方とも同じファイル内にある。外部からの呼び出しは確認されていない。

### 2. `services/patrol/rakuma_patrol.py`（修正）

**変更の要点**:
- `from mercari_db import create_driver` を削除
- `driver=None` の場合に Playwright を使用
- `driver` が渡されても無視する（後方互換のため保持可）

```python
# 移行後のイメージ
class RakumaPatrol(BasePatrol):
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Playwright（Scrapling StealthyFetcher）を使用してラクマの価格・在庫を取得。
        driver 引数は後方互換のために保持するが、使用しない。
        """
        try:
            page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
            body_text = page.get_text()
            
            price = self._extract_price_from_page(page, body_text)
            status = self._extract_status(body_text)
            
            return PatrolResult(price=price, status=status, variants=[])
        except Exception as e:
            logger.error(f"Rakuma patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
```

### 3. `services/scraping_client.py`（修正）

`fetch_dynamic()` を本番対応させる：

```python
def fetch_dynamic(url: str, headless: bool = True, network_idle: bool = True, **kwargs):
    """
    Scrapling StealthyFetcher（Playwright ベース）でページ取得。
    
    前提: Dockerfile に `RUN python -m scrapling install` が追加済みであること。
    """
    from scrapling import StealthyFetcher
    return StealthyFetcher.fetch(
        url,
        headless=headless,
        network_idle=network_idle,
        **kwargs
    )
```

### 4. `Dockerfile`（追加）

`pip install` の直後に Playwright ブラウザをインストール：

```dockerfile
# 依存関係のインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright ブラウザのインストール（scrapling 経由）
# これにより Chromium + 必要なシステムライブラリがインストールされる
RUN python -m scrapling install

# ソースコードのコピー
COPY . .
```

> **重要**: `RUN python -m scrapling install` は `COPY . .` の**前**に実行すること。
> そうすることで、ソースコードの変更時にブラウザの再インストールを避けられる（ Docker キャッシュ活用）。

追加が必要な場合のシステムライブラリ（Playwright の Chromium 依存）：
```dockerfile
# Playwright/Chromiumに必要な場合
RUN apt-get update && apt-get install -y \
    libnss3 libxss1 libasound2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libgtk-3-0 \
    libgbm1 libxcomposite1 libxdamage1 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*
```
（多くは既存の Chrome インストール時にインストール済みのため、重複しても問題ない）

### 5. `services/scrape_queue.py`（修正）

ラクマを `BROWSER_SITES` から削除：

```python
# Stage 1 完了後
BROWSER_SITES = frozenset({"mercari"})  # rakuma を削除
```

---

## Render 互換性チェックリスト

Stage 1 の実装後、Render 本番環境で以下を検証すること：

### デプロイ確認

- [ ] `docker build` が成功する（`RUN python -m scrapling install` が通る）
- [ ] イメージサイズが許容範囲内（~1.5GB 以下）
- [ ] Render へのデプロイが成功する

### 動作確認

- [ ] `/scrape` フォームからラクマ URL を入力してスクレイピングが成功する
- [ ] タイトル、価格、画像が正しく取得できる
- [ ] `MonitorService` のパトロールが正常に動作する（ラクマ商品の価格更新）
- [ ] メモリ使用量が OOM を起こさない（Render ダッシュボードで確認）

### Playwright 固有の確認

- [ ] `/dev/shm` サイズが十分か（Playwright は共有メモリを使用）
  - Render では通常 `/dev/shm` が 64MB。足りない場合は `--disable-dev-shm-usage` を追加
- [ ] サンドボックス設定: `--no-sandbox` フラグが必要（非 root ユーザーでの実行時）
- [ ] メモリ使用量の実測: Chrome（400MB）と Playwright（目標 ~150MB）の比較

Render ダッシュボードでのメモリ確認方法：
`Render Dashboard → Service → Metrics → Memory Usage`

---

## 遅延読み込み画像の対応

ラクマは `data-lazy` または `data-src` 属性に実際の画像 URL を格納している。

### 現在の Selenium 実装（`rakuma_db.py` 97-101行）

```python
src = img.get_attribute("src")
if not src or "placeholder" in src.lower() or "blank" in src.lower():
    src = img.get_attribute("data-lazy") or img.get_attribute("data-src")
```

### Playwright（StealthyFetcher）での対応

```python
# StealthyFetcher は network_idle=True の場合、ページが完全にロードされてから返る
# → 遅延読み込みが完了している可能性が高い

page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
imgs = page.css(".sp-image")  # ラクマの画像セレクタ

for img in imgs:
    src = img.attrib.get("src", "")
    if not src or "placeholder" in src.lower():
        src = img.attrib.get("data-lazy") or img.attrib.get("data-src") or ""
    if src and src.startswith("http"):
        image_urls.append(src)
```

Playwright 直接 API の場合：
```python
# wait_for_selector を使って遅延読み込み完了を待つ
await page.wait_for_selector("img[src*='fril.jp']", timeout=5000)
# または
await page.wait_for_load_state("networkidle")
```

---

## テスト要件

### ユニットテスト（`tests/test_rakuma_playwright.py` 新規作成）

```python
import pytest
from unittest.mock import patch, MagicMock


def test_scrape_item_detail_structure():
    """scrape_item_detail が正しい構造を返すことをモックで確認"""
    mock_page = MagicMock()
    mock_page.css_first.return_value = MagicMock(text="テスト商品タイトル")
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from rakuma_db import scrape_item_detail
        result = scrape_item_detail("https://item.fril.jp/test")
    
    assert "title" in result
    assert "price" in result
    assert "status" in result
    assert "image_urls" in result
    assert isinstance(result["image_urls"], list)


def test_rakuma_patrol_uses_playwright():
    """RakumaPatrol が Selenium ではなく Playwright を使用することを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥1,000 テスト商品"
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from services.patrol.rakuma_patrol import RakumaPatrol
        patrol = RakumaPatrol()
        result = patrol.fetch("https://item.fril.jp/test")
    
    assert result.success
    assert result.price == 1000
```

### 統合テスト（本番環境または Render 環境で実施）

```bash
# 実際のラクマURLでスクレイピングをテスト
python -c "
from rakuma_db import scrape_single_item
items = scrape_single_item('https://item.fril.jp/xxx')
print(items)
"
```

---

## 引き継ぎドキュメント: `docs/specs/STAGE_1_RESULTS.md`

Stage 1 完了後、以下のテンプレートで結果をドキュメント化すること：

```markdown
# Stage 1 実施結果

## 実施日: YYYY-MM-DD
## 実施者: [Agent名/担当者]

## Render 互換性検証結果

### Playwright 動作確認
- [ ] Chromium インストール成功
- [ ] ラクマ商品ページ取得成功
- [ ] 遅延読み込み画像取得成功
- [ ] メモリ使用量: [実測値] MB（目標: ~150MB）

### 確認したラクマ URL
- URL: https://item.fril.jp/xxx
- 取得タイトル: [タイトル]
- 取得価格: [価格]円
- 画像数: [枚数]

### 問題点と対処

[問題があれば記載]

## メルカリへの引き継ぎ事項

[Stage 2 の担当者への注意事項]
```

---

## 次の Agent への引き継ぎ（Stage 2 の担当者へ）

Stage 1 完了後に伝えるべき事項：

1. **`docs/specs/STAGE_1_RESULTS.md` を必ず読んでください**
   - Render での Playwright メモリ使用量の実測値
   - `/dev/shm` に関する問題と対処法
   - Bot 検知に関する観察事項

2. **Playwright の設定**:
   - Render 環境で必要な起動オプション（`--no-sandbox`, `--disable-dev-shm-usage` など）
   - Stage 1 で確認した最適なタイムアウト値

3. **メルカリの Bot 検知について**:
   - ラクマと比べてメルカリはより厳格な Bot 検知を行う
   - `StealthyFetcher` のオプション（`block_webrtc=True`, カスタム User-Agent など）を試すこと
   - 検知された場合の対処法として、`network_idle=True` + 追加待機を試すこと

4. **`services/scrape_queue.py` の更新確認**:
   - `BROWSER_SITES` から `"rakuma"` が削除されていることを確認

5. **Dockerfile の確認**:
   - `RUN python -m scrapling install` が正しく追加されていること
   - Stage 2 では追加の変更は不要（同じ Dockerfile を使用）
