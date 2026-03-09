# Stage 3 実施結果

## 実施日: 2026-03-09
## 実施者: GitHub Copilot Coding Agent

## 変更内容

### 1. `mercari_db.py`（全面書き換え）

- Selenium の import をすべて削除（`selenium`, `webdriver_manager`, `undetected_chromedriver`）
- `create_driver()` 関数を削除
- `_get_chrome_version()` 関数を削除
- `scrape_item_detail()` → `StealthyFetcher.fetch()` を使用
- `scrape_shops_product()` → `StealthyFetcher.fetch()` を使用（バリエーション取得含む）
- `scrape_search_result()` → Playwright async API を使用（スクロール対応）
- `scrape_single_item()` → Playwright async API を使用

### 2. `services/scrape_queue.py`（修正）

- `BROWSER_SITES` から `"mercari"` を削除: `frozenset({"mercari"})` → `frozenset()`
- メルカリは `http_executor` で処理されるようになった（StealthyFetcher 内部で Playwright を使用）

## 実績値

| 指標                  | 移行前    | 移行後    |
|-----------------------|-----------|-----------|
| BROWSER_SITES の件数  | 1 (mercari) | 0 (空集合) |
| mercari_db.py の Selenium import | あり | なし |
| `create_driver()` の有無 | あり | なし |

## Bot 検知への対処

- `StealthyFetcher` の `block_webrtc=True`, `network_idle=True` 設定で対応
- 検索スクロールは `playwright.async_api` を直接使用（`async_playwright()` コンテキストマネージャー）
- `--no-sandbox`, `--disable-dev-shm-usage` フラグを Chromium 起動引数に追加（Render 環境対応）

## 既知の問題

- `asyncio` と Flask/Gunicorn スレッドプールの相性: `_get_or_create_event_loop()` ヘルパーで対処
- 非常に長いスクロールが必要な検索では Playwright が StealthyFetcher より遅い場合がある

## Stage 4 への引き継ぎ事項

1. **`BROWSER_SITES` は空集合** — 全サイトが `http_executor` で処理される
2. **残存 Selenium コード**（Stage 4 で削除予定）:
   - `requirements.txt`: `selenium`, `webdriver-manager`, `undetected-chromedriver`
   - `Dockerfile`: Google Chrome インストールブロック
   - `services/patrol/` 各ファイル: `_fetch_with_selenium()` メソッド（dead code）
   - `surugaya_db.py`: `_fetch_soup_with_selenium()` 関数
   - `yahoo_db.py`, `yahuoku_db.py`, `snkrdunk_db.py`, `offmall_db.py`: Selenium ベースの DB スクレイパー
3. **注意**: `yahoo_db.py` 等の DB スクレイパーには Selenium が残っているが、
   `scrape_queue.py` の `BROWSER_SITES` が空のため `browser_executor` は使用されない。
   ただし `http_executor` から直接これらの関数が呼ばれる場合は Selenium が実行される可能性がある。
   完全な Selenium 削除は Stage 4 で行う。
