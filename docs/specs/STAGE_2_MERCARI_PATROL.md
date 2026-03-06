# Stage 2: メルカリパトロール Playwright 移行仕様書

## 読むべきドキュメント

1. [CURRENT_ARCHITECTURE.md](./CURRENT_ARCHITECTURE.md) — 現在のコードベース構造
2. [STAGE_0_QUEUE_SYSTEM.md](./STAGE_0_QUEUE_SYSTEM.md) — キューシステム仕様
3. [STAGE_1_RAKUMA_PLAYWRIGHT.md](./STAGE_1_RAKUMA_PLAYWRIGHT.md) — ラクマ移行仕様
4. **`docs/specs/STAGE_1_RESULTS.md`**（Stage 1 実施後に作成） — Render 互換性検証結果 ← **必読**

---

## 前提条件

- **Stage 1 完了済み**: ラクマが Playwright で動作し、Render 互換性が確認されている
- **`docs/specs/STAGE_1_RESULTS.md` が存在する**: Render での Playwright メモリ使用量、設定が記載されている

> ⚠️ **STAGE_1_RESULTS.md が存在しない場合は Stage 1 が未完了です。先に Stage 1 を実施してください。**

---

## 目標

`services/patrol/mercari_patrol.py`（`MercariPatrol` クラス）を Selenium から Playwright に移行する。

**スコープ**: パトロール（価格・在庫監視）のみ。検索スクロールや商品詳細の完全取得は Stage 3 の作業。

---

## 現在のコード分析

### `services/patrol/mercari_patrol.py`（~149行）

```python
# 現在のインポート（削除対象）
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
```

**主要な Selenium 使用箇所（行番号は概算）**:

| 関数名                 | Selenium 使用箇所                                              |
|------------------------|---------------------------------------------------------------|
| `fetch()`              | `driver.get(url)`, `WebDriverWait(driver, 8)`, `time.sleep(1)` |
| `_extract_price()`     | `driver.find_elements(By.CSS_SELECTOR, "[data-testid='price']")` |
| `_extract_status()`    | `driver.find_element(By.TAG_NAME, "body").text`, `driver.find_elements(By.CSS_SELECTOR, "button")` |
| `_extract_variants()`  | `driver.find_elements(By.CSS_SELECTOR, "[data-testid='variation-label']")` |

**`_extract_variants` の重要な Selenium 特有コード**:

```python
# 現在のコード（mercari_patrol.py 133-136行付近）
is_sold = (
    "売り切れ" in label.get_attribute("innerHTML") or
    "disabled" in label.get_attribute("class")
)
```

この `innerHTML` と `class` の取得方法が変わる（下記の API 対応表を参照）。

### `services/monitor_service.py`

```python
# 現在の設定
_BROWSER_SITES = frozenset({"mercari"})

# check_stale_products() 内
if product.site in _BROWSER_SITES:
    if driver is None:
        from mercari_db import create_driver
        driver = create_driver(headless=True)
    result = patrol.fetch(product.source_url, driver=driver)
```

`monitor_service.py` は `MercariPatrol` に共有 `driver` を渡すことで、複数商品の監視に
1つの Chrome インスタンスを再利用している。Playwright 移行後はこの shared driver ロジックを変更する必要がある。

---

## Selenium → Playwright API 対応表（メルカリパトロール特有）

| Selenium（現在）                                              | Playwright / Scrapling（移行後）                                    |
|---------------------------------------------------------------|---------------------------------------------------------------------|
| `driver.get(url)` + `WebDriverWait(...).until(...)` + `time.sleep(1)` | `StealthyFetcher.fetch(url, network_idle=True)` |
| `driver.find_element(By.TAG_NAME, "body").text`               | `page.get_text()`（Scrapling）または `await page.inner_text("body")` |
| `driver.find_elements(By.CSS_SELECTOR, "[data-testid='price']")` | `page.css("[data-testid='price']")`（Scrapling）                 |
| `driver.find_elements(By.CSS_SELECTOR, "button")`             | `page.css("button")`                                               |
| `btn.is_enabled()`                                            | `await btn.is_disabled()`（**反転に注意**）                         |
| `label.get_attribute("innerHTML")`                            | `await label.inner_html()` または `label.html`（Scrapling）        |
| `label.get_attribute("class")`                                | `await label.get_attribute("class")` または `label.attrib.get("class")` |

### 重要な注意: `is_enabled()` → `is_disabled()` の反転

```python
# Selenium（現在）
if btn.is_enabled():
    return "active"
else:
    return "sold"

# Playwright（移行後） ← is_disabled() は is_enabled() の逆
is_disabled = await btn.is_disabled()
if not is_disabled:  # == is_enabled()
    return "active"
else:
    return "sold"
```

---

## 変更するファイル

### 1. `services/patrol/mercari_patrol.py`（修正）

**完全書き換えのイメージ**:

```python
"""
Mercari lightweight patrol scraper.
Playwright（Scrapling StealthyFetcher）を使用。
Stage 2で Selenium から移行。
"""
import re
import logging
from typing import Optional
from scrapling import StealthyFetcher

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.mercari")


class MercariPatrol(BasePatrol):
    """Lightweight Mercari price/stock scraper using Playwright."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Playwright（StealthyFetcher）でメルカリの価格・在庫を取得。
        
        driver 引数は後方互換のために保持するが、使用しない。
        monitor_service.py の _BROWSER_SITES から "mercari" が削除された後は
        driver が渡されなくなる。
        """
        try:
            page = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=True,  # JS ロード完了を待機
            )
            
            body_text = page.get_text()
            
            price = self._extract_price(page, body_text)
            status = self._extract_status(page, body_text)
            variants = self._extract_variants(page)
            
            return PatrolResult(
                price=price,
                status=status,
                variants=variants,
            )
            
        except Exception as e:
            logger.error(f"Patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
    
    def _extract_price(self, page, body_text: str) -> Optional[int]:
        """Scrapling の CSS セレクタで価格を取得"""
        # data-testid='price' を優先
        price_el = page.css_first("[data-testid='price']")
        if price_el:
            price_text = price_el.text
            m = re.search(r"[¥￥]\s*([\d,]+)", price_text) or re.search(r"([\d,]+)", price_text)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        
        # フォールバック: body テキストから regex
        if body_text:
            m = re.search(r"[¥￥]\s*([\d,]+)", body_text)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        
        return None
    
    def _extract_status(self, page, body_text: str) -> str:
        """ページのテキストとボタン状態からステータスを判定"""
        if "売り切れ" in body_text or "Sold" in body_text:
            return "sold"
        
        # ボタンの状態チェック（Scrapling では attrib で確認）
        buttons = page.css("button")
        for btn in buttons:
            btn_text = btn.text.lower() if btn.text else ""
            if "購入" in btn_text or "buy" in btn_text:
                # disabled 属性の確認
                disabled = btn.attrib.get("disabled")
                aria_disabled = btn.attrib.get("aria-disabled", "false")
                if disabled is None and aria_disabled != "true":
                    return "active"
                else:
                    return "sold"
        
        return "active" if body_text else "unknown"
    
    def _extract_variants(self, page) -> list:
        """
        メルカリShopsのバリエーション情報を取得。
        Scrapling の CSS セレクタと attrib を使用。
        """
        variants = []
        
        var_labels = page.css("[data-testid='variation-label']")
        for label in var_labels:
            name = label.text.strip() if label.text else ""
            
            # HTML 内容で売り切れを確認
            label_html = label.html or ""
            label_class = label.attrib.get("class", "")
            
            is_sold = "売り切れ" in label_html or "disabled" in label_class
            
            variants.append({
                "name": name,
                "stock": 0 if is_sold else 1,
                "price": None,
            })
        
        return variants
```

### 2. `services/monitor_service.py`（修正）

**変更点**: 
1. `_BROWSER_SITES` から `"mercari"` を削除（Playwright 移行により不要）
2. shared driver ロジック（`create_driver()` の呼び出し）を削除

```python
# Stage 2 完了後
_BROWSER_SITES = frozenset()  # 全サイトがHTTP/Playwright対応

# check_stale_products() 内の変更
for product in products:
    patrol = MonitorService._patrols.get(product.site)
    if not patrol:
        continue
    
    # 全サイトが driver 不要になった
    result = patrol.fetch(product.source_url)  # driver 引数を渡さない
    
    # ... (残りは同じ)
```

**削除するコード**:
```python
# 以下を削除
if product.site in _BROWSER_SITES:
    if driver is None:
        from mercari_db import create_driver
        driver = create_driver(headless=True)
    result = patrol.fetch(product.source_url, driver=driver)
else:
    result = patrol.fetch(product.source_url, driver=None)
```

```python
# finally ブロックから削除
finally:
    if driver:
        try:
            driver.quit()  # ← 削除
        except Exception:
            pass
    session_db.close()
```

---

## 重要: パトロールのみ（検索スクロールは不要）

Stage 2 の `MercariPatrol` は以下を取得するだけでよい：

- ✅ 価格（`[data-testid='price']`）
- ✅ ステータス（売り切れ判定）
- ✅ バリエーション在庫（メルカリShopsのみ）

以下は **Stage 2 のスコープ外**（Stage 3 で対応）：

- ❌ 検索結果のスクロール取得
- ❌ 商品タイトル・説明文・画像
- ❌ `scrape_item_detail()` の書き換え

---

## StealthyFetcher のオプション設定

Stage 1 の `STAGE_1_RESULTS.md` を参照して、Render 環境で検証済みの設定を使用すること。

一般的に必要なオプション：

```python
StealthyFetcher.fetch(
    url,
    headless=True,          # ヘッドレスモード
    network_idle=True,      # ネットワークアイドル待機（JS ロード完了）
    # 必要に応じて以下を追加:
    # block_webrtc=True,    # WebRTC ブロック（プライバシー強化）
    # disable_resources=["image", "font"],  # 不要リソースのブロック（高速化）
)
```

メルカリはより厳しい Bot 検知を行うため、`STAGE_1_RESULTS.md` の観察事項を確認すること。

---

## テスト要件

### `tests/test_mercari_patrol_playwright.py` 新規作成

```python
import pytest
from unittest.mock import patch, MagicMock


def _make_mock_page(
    body_text="¥1,000 テスト商品",
    price_text="¥1,000",
    is_sold=False,
):
    """テスト用モックページオブジェクトを作成"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = body_text
    
    # price 要素
    price_el = MagicMock()
    price_el.text = price_text
    mock_page.css_first.return_value = price_el
    
    # buttons
    btn = MagicMock()
    btn.text = "購入手続きへ"
    btn.attrib = {"disabled": None} if not is_sold else {"disabled": ""}
    mock_page.css.return_value = [btn]
    
    return mock_page


def test_fetch_active_product():
    """販売中の商品を正しく取得できることを確認"""
    mock_page = _make_mock_page()
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol
        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")
    
    assert result.success
    assert result.price == 1000
    assert result.status == "active"


def test_fetch_sold_product():
    """売り切れ商品を正しく判定できることを確認"""
    mock_page = _make_mock_page(body_text="売り切れ ¥1,000")
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol
        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")
    
    assert result.success
    assert result.status == "sold"


def test_fetch_error_handling():
    """ネットワークエラー時に PatrolResult(error=...) を返すことを確認"""
    with patch("scrapling.StealthyFetcher.fetch", side_effect=Exception("Connection error")):
        from services.patrol.mercari_patrol import MercariPatrol
        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")
    
    assert not result.success
    assert result.error is not None


def test_monitor_service_no_driver():
    """monitor_service が driver を作成しなくなったことを確認"""
    # Stage 2 完了後、_BROWSER_SITES は空集合のはず
    from services.monitor_service import _BROWSER_SITES
    assert "mercari" not in _BROWSER_SITES
```

### 既存テストの確認

`tests/test_scraping_logic.py` の既存テストが `mercari_db.py` に依存していることを確認し、
`monitor_service.py` のテストがある場合はそれも更新すること。

---

## 次の Agent への引き継ぎ（Stage 3 の担当者へ）

Stage 2 完了後、Stage 3 の担当者が知るべき重要事項：

### Mercari の Bot 検知挙動

Stage 2 でパトロール（単一 URL の取得）を実施した際に観察された Bot 検知に関する事項：

1. **StealthyFetcher の有効性**: メルカリの Bot 検知に対して有効だったか？
2. **CAPTCHA の発生**: テスト中に CAPTCHA が表示された場合は記録すること
3. **最適なオプション設定**: `network_idle`, `block_webrtc` などの設定でどの組み合わせが最良だったか
4. **レスポンス速度**: 1商品あたりの取得時間（秒）

これらを `docs/specs/STAGE_2_RESULTS.md` に記録し、Stage 3 の担当者に引き継ぐこと。

### Stage 3 での最大の技術的課題

Stage 3 では `mercari_db.py`（~608行）を完全書き換えする。特に以下が難題：

1. **検索スクロール**: `window.scrollTo()` + ループが Scrapling の StealthyFetcher で使えない
   - → Playwright 直接 API（`async_playwright`）を使う必要がある
   - Stage 2 で `asyncio` + Playwright の動作を確認しておくと良い

2. **メルカリShopsのバリエーション取得**: 複雑な DOM 操作
   - `driver.execute_script("return arguments[0].nextElementSibling", parent)` の置き換え
   - → `await parent.evaluate("el => el.nextElementSibling")` を試すこと

3. **メモリ管理**: Playwright ブラウザのコンテキスト管理
   - 1リクエストごとにコンテキストを作成・削除する設計を推奨
   - Stage 1 で確認したメモリ実測値を参考に

### `services/scrape_queue.py` の現状確認

`BROWSER_SITES` の値を確認：
- Stage 1 完了後: `frozenset({"mercari"})` のはず
- Stage 3 完了後: `frozenset()` に変更予定
