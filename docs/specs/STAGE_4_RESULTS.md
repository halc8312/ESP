# Stage 4b 完了記録

> **完了日**: 2026-03-10  
> **対象**: DB スクレイピング層 Selenium 削除

---

## 1. 完了した内容

### コード

- `yahoo_db.py` から Selenium / `create_driver()` を削除
- `offmall_db.py`, `snkrdunk_db.py`, `yahuoku_db.py`, `surugaya_db.py` の Selenium 依存を削除
- `services/patrol/yahoo_patrol.py`
- `services/patrol/offmall_patrol.py`
- `services/patrol/snkrdunk_patrol.py`
- `services/patrol/yahuoku_patrol.py`
- `services/patrol/surugaya_patrol.py`
  - 上記 patrol 実装の `_fetch_with_selenium()` デッドコードを削除
- `debug_scrape.py`, `debug_children.py`, `debug_variant_json.py` を削除

### 依存関係 / 実行環境

- `requirements.txt` から `selenium`, `webdriver-manager`, `undetected-chromedriver` を削除
- `Dockerfile` から Chrome の追加インストール処理を削除
- `Dockerfile` の Gunicorn 設定を `--workers 1 --threads 8 --max-requests 0` 前提へ整理

### 互換性調整

- `app.py` に Windows 向け `fcntl` 非依存分岐を追加
- `rakuma_db.py` に既存テスト互換の補助処理を追加

---

## 2. 検証結果

実行確認:

- `pytest tests/test_stage4_selenium_removal.py -q`
- `pytest tests/test_rakuma_playwright.py tests/test_scrape_queue.py -q`
- `pytest tests/test_scraping_logic.py -q`
- `pytest tests -q`

静的確認:

- `rg -n "selenium|create_driver|webdriver_manager|undetected_chromedriver" . --glob "*.py" --glob "Dockerfile" --glob "requirements.txt" -g "!tests/*"`
  - 本体コード側の該当なし

---

## 3. 現在の前提

- DB スクレイピング層は Selenium を前提にしない
- patrol 層の対象サイトも driver 引数に依存しない
- `services.scrape_queue.BROWSER_SITES = frozenset()`
- `services.monitor_service._BROWSER_SITES = frozenset()`

---

## 4. 未実施のもの

Stage 4b の完了判定には含めないが、別タスクとして残るもの:

- 実サイト相手の自動スモークテスト整備
- Block C / D の未実装機能
- ローカル `.venv` に残っている旧 Selenium 系パッケージの掃除

---

## 5. 参照

- [`docs/UNIFIED_ROADMAP.md`](../UNIFIED_ROADMAP.md)
- [`docs/work_reports/20260310/block_a_stage4b_report.md`](../work_reports/20260310/block_a_stage4b_report.md)
- [`docs/work_reports/20260310/deep_audit_after_block_a.md`](../work_reports/20260310/deep_audit_after_block_a.md)
