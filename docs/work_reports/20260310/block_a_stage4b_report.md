# 2026-03-10 作業報告書

## 対象
- 統合ロードマップ: `docs/UNIFIED_ROADMAP.md`
- 今回の実施範囲: Block A `Selenium 完全削除`（Stage 4b）

## 事前に確認した主な資料
- `docs/UNIFIED_ROADMAP.md`
- `docs/specs/CURRENT_ARCHITECTURE.md`
- `docs/specs/STAGE_4_SELENIUM_REMOVAL.md`
- `docs/specs/STAGE_1_RESULTS.md`
- `docs/specs/README.md`
- `DEVELOPMENT_STATUS.md`
- `reports.md`
- 関連実装: `routes/scrape.py`, `services/monitor_service.py`, 各 DB スクレイパー, 各 patrol 実装

## 完了した作業

### A-1. デバッグスクリプト削除
以下を削除しました。
- `debug_scrape.py`
- `debug_children.py`
- `debug_variant_json.py`

`debug_yahoo_repro.py` は Selenium 非依存のまま残置しています。

### A-2. patrol 層のデッドコード削除
以下の patrol から `_fetch_with_selenium()` を撤去し、`fetch(url, driver=None)` は driver を無視して Scrapling 経路へ固定しました。
- `services/patrol/yahoo_patrol.py`
- `services/patrol/offmall_patrol.py`
- `services/patrol/snkrdunk_patrol.py`
- `services/patrol/yahuoku_patrol.py`
- `services/patrol/surugaya_patrol.py`

後方互換のため、`driver` 引数自体は残しています。

### A-3. `yahoo_db.py` の Selenium 除去
- Selenium / ChromeDriver / WebDriverManager import を削除
- `create_driver()` を削除
- `scrape_item_detail()` を HTTP 実装へ統一
- 旧シグネチャ `(driver, url)` を壊さないよう互換ラッパー化
- `scrape_single_item()` を HTTP 専用化
- `scrape_search_result()` を HTTP 検索ページ解析へ置換

### A-4. `offmall_db.py` / `snkrdunk_db.py` / `yahuoku_db.py` の Selenium 除去
各モジュールで共通して以下を実施しました。
- `from yahoo_db import create_driver` を削除
- Selenium import を削除
- `scrape_item_detail()` を Scrapling 実装へ統一
- `scrape_single_item()` をブラウザ生成なしへ変更
- `scrape_search_result()` を HTTP もしくは Scrapling dynamic fetch へ置換

補足:
- `snkrdunk_db.py` の検索結果取得は Selenium の代わりに `fetch_dynamic()` を優先使用します。

### A-5. `surugaya_db.py` の Selenium 除去
- `_should_use_selenium_fallback()` を削除
- `_fetch_soup_with_selenium()` を削除
- 商品詳細 / 検索の Selenium フォールバック分岐を撤去
- 既存の Yahoo 検索フォールバック / global domain フォールバックは維持

### A-6. `requirements.txt` / `Dockerfile` のクリーンアップ
以下を削除しました。
- `selenium`
- `webdriver-manager`
- `undetected-chromedriver`

`Dockerfile` では以下を削除しました。
- Chrome インストールブロック全体
- `wget`, `gnupg`, `unzip` の導入

Playwright / Patchright / Scrapling 前提の構成へ整理しました。

## あわせて行った保守対応

### Windows テスト環境の互換修正
`app.py` の `fcntl` import が Windows で失敗し、`tests/conftest.py` 経由の pytest 実行が止まっていたため、以下を追加しました。
- `fcntl` が存在しない環境ではロックなしで scheduler を起動する分岐
- Linux / 本番のロック処理は維持

### 既存テストの現行実装追従
現行アーキテクチャに合わせて以下を補正しました。
- `rakuma_db.py`: 既存モック (`css_first`) にも対応する小さな互換ヘルパー追加
- `tests/test_rakuma_playwright.py`: `mercari_db.create_driver` 不在前提へ更新
- `tests/test_scrape_queue.py`: `BROWSER_SITES = frozenset()` 前提へ更新

### 新規テスト追加
- `tests/test_stage4_selenium_removal.py`
  - Stage 4b 対象モジュールに Selenium import が残っていないこと
  - patrol の `driver` 引数が無視されること
  - Yahoo / Offmall / Yahuoku / SNKRDUNK / Surugaya の新経路が最低限動作すること
  - デバッグスクリプトが削除済みであること

## 検証結果

### 依存残存確認
以下の検索で、`tests/` を除くコードベースに Selenium 系文字列が残っていないことを確認しました。
- `rg -n "selenium|create_driver|webdriver_manager|undetected_chromedriver" . --glob "*.py" --glob "Dockerfile" --glob "requirements.txt" -g '!tests/*'`

結果: 該当なし

### 実行したテスト
- `pytest tests/test_stage4_selenium_removal.py -q`
  - 結果: `8 passed`
- `pytest tests/test_rakuma_playwright.py tests/test_scrape_queue.py -q`
  - 結果: `29 passed`
- `pytest tests/test_scraping_logic.py -q`
  - 結果: `3 passed`
  - 備考: `mercari_db.py` の event loop 取得に関する DeprecationWarning が 1 件出るが、失敗ではない

## 変更ファイル（主要）
- `yahoo_db.py`
- `offmall_db.py`
- `snkrdunk_db.py`
- `yahuoku_db.py`
- `surugaya_db.py`
- `services/patrol/yahoo_patrol.py`
- `services/patrol/offmall_patrol.py`
- `services/patrol/snkrdunk_patrol.py`
- `services/patrol/yahuoku_patrol.py`
- `services/patrol/surugaya_patrol.py`
- `requirements.txt`
- `Dockerfile`
- `app.py`
- `tests/test_stage4_selenium_removal.py`
- `tests/test_rakuma_playwright.py`
- `tests/test_scrape_queue.py`
- `rakuma_db.py`

## ロードマップ上の進捗整理
- Block A: 完了
- Block B: コード確認の結果、`templates/index.html` と `templates/scrape_form.html` には既に B-1〜B-7 相当の実装がかなり入っていることを確認
- Block B-8 (`templates/product_detail.html` のさらなるコンパクト化): 今回は未着手
- Block C / D: 未着手

## 注意点
- `docs/specs/STAGE_3_RESULTS.md` は参照先として仕様書に記載されているが、現リポジトリ内には存在しませんでした
- ブラウザ依存は Selenium からは撤去済みですが、`snkrdunk_db.py` の検索は Scrapling dynamic fetch を使うため Playwright 系ブラウザ資産は引き続き必要です
