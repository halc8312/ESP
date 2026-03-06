# Stage 1 実施結果

## 実施日: 2026-03-06
## 実施者: GitHub Copilot Coding Agent

## 変更内容

### 1. `rakuma_db.py`（全面書き換え）
- Selenium の import をすべて削除（`selenium`, `mercari_db.create_driver`）
- `scrape_item_detail(url, driver=None)` → Scrapling `StealthyFetcher.fetch()` を使用
  - `driver` 引数は後方互換のため保持するが使用しない
- `scrape_single_item()` → Selenium ドライバー作成を削除
- `scrape_search_result()` → Playwright async API を使用（スクロール対応）
  - `_get_or_create_event_loop()` パターンでスレッドセーフなイベントループ取得

### 2. `services/patrol/rakuma_patrol.py`（修正）
- Selenium の import をすべて削除
- `RakumaPatrol.fetch()` → `StealthyFetcher.fetch()` を使用
- `_extract_price()` → `_extract_price_from_page()` に変更（Scrapling page オブジェクト対応）

### 3. `services/scraping_client.py`（修正）
- `fetch_dynamic()` に `headless` と `network_idle` パラメータを追加

### 4. `Dockerfile`（追加）
- `RUN python -m scrapling install` を pip install の直後、`COPY . .` の前に追加
- Docker キャッシュを活用するための配置

### 5. `services/scrape_queue.py`（修正）
- `BROWSER_SITES` から `"rakuma"` を削除: `frozenset({"mercari", "rakuma"})` → `frozenset({"mercari"})`
- ラクマは `http_executor` で処理されるようになった

### 6. テスト
- `tests/test_rakuma_playwright.py` を新規作成（18テストケース）
- `tests/test_scrape_queue.py` を更新（rakuma の BROWSER_SITES 分類を変更）

## Render 互換性検証結果

### Playwright 動作確認
- [ ] Chromium インストール成功（`RUN python -m scrapling install` がDockerfileに追加済み）
- [ ] ラクマ商品ページ取得成功
- [ ] 遅延読み込み画像取得成功
- [ ] メモリ使用量: [実測値] MB（目標: ~150MB）

### 確認済み Playwright 設定
- `--no-sandbox` フラグ（Render 非 root ユーザー対応）
- `--disable-dev-shm-usage` フラグ（`/dev/shm` サイズ制限対応）
- `--disable-gpu` フラグ
- `network_idle=True` でページロード完了を待機

### 問題点と対処

- スレッドプールから `asyncio.run()` を呼ぶと `RuntimeError` が発生する場合がある
  → `_get_or_create_event_loop()` パターンで対応

## メルカリへの引き継ぎ事項（Stage 2 の担当者へ）

1. **Playwright の設定**:
   - Render 環境で `--no-sandbox`, `--disable-dev-shm-usage` が必要
   - `network_idle=True` でページロード待機

2. **メルカリの Bot 検知について**:
   - ラクマと比べてメルカリはより厳格な Bot 検知を行う
   - `StealthyFetcher` のオプション（`block_webrtc=True`, カスタム User-Agent 等）を試すこと

3. **`services/scrape_queue.py` の確認**:
   - `BROWSER_SITES` から `"rakuma"` が削除済み
   - Stage 2 でメルカリパトロールを移行後、Stage 3 で `"mercari"` も削除予定

4. **Dockerfile の確認**:
   - `RUN python -m scrapling install` が正しく追加済み
   - Stage 2 では追加の Dockerfile 変更は不要
