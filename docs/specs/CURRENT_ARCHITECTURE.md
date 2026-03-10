# 現在のアーキテクチャ概要

> **更新日**: 2026-03-10  
> **対象読者**: 現在のコードベースを前提に追加実装・保守を行う作業者

---

## 1. リポジトリ構成

```
ESP/
├── app.py                          # Flask アプリ本体、Blueprint 登録、軽量マイグレーション、Scheduler 起動
├── models.py                       # SQLAlchemy ORM（Product, Variant, Shop, User, Template など）
├── database.py                     # DB 接続・SessionLocal
├── requirements.txt                # Python 依存ライブラリ
├── Dockerfile                      # Python 3.11 slim + Playwright/Patchright 実行環境
│
├── mercari_db.py                   # メルカリスクレイパー（StealthyFetcher / Playwright）
├── rakuma_db.py                    # ラクマスクレイパー（検索: Playwright、詳細: HTTP Fetcher）
├── yahoo_db.py                     # Yahoo Shopping スクレイパー（HTTP / Scrapling）
├── yahuoku_db.py                   # ヤフオクスクレイパー（HTTP / Scrapling）
├── surugaya_db.py                  # 駿河屋スクレイパー（HTTP / Scrapling）
├── offmall_db.py                   # オフモールスクレイパー（HTTP / Scrapling）
├── snkrdunk_db.py                  # SNKRDUNK スクレイパー（詳細: HTTP、検索: dynamic fetch）
│
├── routes/
│   ├── main.py                     # 一覧・ダッシュボード
│   ├── products.py                 # 商品詳細編集・商品更新
│   ├── scrape.py                   # 商品抽出フォーム、キュー投入、待機画面、結果画面
│   ├── api.py                      # 非同期ポーリング API、商品一覧のインライン更新 API
│   └── ...
│
├── services/
│   ├── scrape_queue.py             # インメモリ ScrapeQueue
│   ├── scraping_client.py          # fetch_static / fetch_dynamic ラッパー
│   ├── monitor_service.py          # 定期パトロール
│   ├── product_service.py          # 抽出結果の DB 保存
│   ├── filter_service.py           # 除外フィルタ
│   └── patrol/
│       ├── mercari_patrol.py       # メルカリ軽量パトロール
│       ├── rakuma_patrol.py        # ラクマ軽量パトロール
│       ├── yahoo_patrol.py         # Yahoo 軽量パトロール
│       ├── yahuoku_patrol.py       # ヤフオク軽量パトロール
│       ├── surugaya_patrol.py      # 駿河屋軽量パトロール
│       ├── offmall_patrol.py       # オフモール軽量パトロール
│       └── snkrdunk_patrol.py      # SNKRDUNK 軽量パトロール
│
├── templates/
│   ├── index.html                  # 商品一覧
│   ├── product_manual_add.html     # 商品手動追加
│   ├── product_detail.html         # 商品編集
│   ├── pricelist_analytics.html    # 価格表アクセス解析
│   ├── scrape_form.html            # 商品抽出フォーム
│   ├── scrape_waiting.html         # 抽出待機画面
│   └── scrape_result.html          # 抽出結果画面
│
├── tests/
│   ├── test_scrape_queue.py
│   ├── test_rakuma_playwright.py
│   ├── test_scraping_logic.py
│   └── test_stage4_selenium_removal.py
│
└── docs/
    ├── UNIFIED_ROADMAP.md
    └── specs/
```

---

## 2. サイト別スクレイピング方式

| サイト | ドメイン | 現在の方式 | 補足 |
|--------|----------|------------|------|
| メルカリ | `jp.mercari.com` | Scrapling `StealthyFetcher` / Playwright | 検索・詳細とも Selenium 依存なし |
| ラクマ | `fril.jp`, `item.fril.jp` | 検索は Playwright、詳細は HTTP Fetcher | 検索はスクロール対応、詳細は browser 不要 |
| Yahoo Shopping | `shopping.yahoo.co.jp` | HTTP (`fetch_static`) | 詳細・検索とも HTTP |
| ヤフオク | `auctions.yahoo.co.jp` | HTTP (`fetch_static`) | 詳細・検索とも HTTP |
| 駿河屋 | `suruga-ya.jp` | HTTP (`fetch_static`) | Selenium フォールバックは削除済み |
| オフモール | `netmall.hardoff.co.jp` | HTTP (`fetch_static`) | JSON-LD 解析中心 |
| SNKRDUNK | `snkrdunk.com` | 詳細は HTTP、検索は `fetch_dynamic` 優先 | dynamic fetch 失敗時は静的取得へフォールバック |

---

## 3. 依存ライブラリ

現在の `requirements.txt` の主な依存は以下の通り:

```
Flask
SQLAlchemy
Flask-Login
Flask-APScheduler
gunicorn
scrapling
playwright
patchright
browserforge
curl_cffi
beautifulsoup4
pandas
requests
msgspec
pytest
pytest-flask
```

補足:

- Selenium / webdriver-manager / undetected-chromedriver は Stage 4b で削除済み
- ブラウザ制御は Scrapling + Playwright / Patchright 系へ統一

---

## 4. デプロイ構成

### Docker

- ベースイメージ: `python:3.11-slim`
- Chrome の個別インストールは行わない
- `scrapling install` と `patchright install chromium` を build 時に実行
- `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` を共有利用

### Gunicorn

`Dockerfile` の実行コマンド:

```bash
gunicorn --worker-class gthread --workers 1 --threads 8 --max-requests 0 --timeout 600 --bind 0.0.0.0:${PORT:-10000} app:app
```

理由:

- `ScrapeQueue` がプロセス内シングルトンのため `--workers 1` を維持
- `--max-requests 0` でワーカー再起動によるバックグラウンドジョブ消失を防止
- `gthread` により待機画面ポーリング中も他リクエストを捌ける

---

## 5. 商品抽出フロー

### エンドポイント

| ルート | 役割 |
|--------|------|
| `/scrape` | 商品抽出フォーム表示 |
| `/scrape/run` | フォーム入力をジョブ化してキュー投入 |
| `/scrape/status/<job_id>` | 待機画面表示 |
| `/scrape/register-selected` | プレビュー結果から選択した商品だけ DB 登録 |
| `/products/manual-add` | スクレイピングなしで商品を初期登録 |
| `/api/scrape/status/<job_id>` | ポーリング用ステータス JSON |
| `/api/products/<id>/inline-update` | 商品一覧の `selling_price` / `custom_title_en` を PATCH 更新 |
| `/api/products/bulk-price` | 選択商品の `selling_price` を一括更新 |
| `/scrape/result/<job_id>` | 完了後の結果画面表示 |
| `/catalog/<token>/product/<product_id>` | 公開カタログの詳細モーダル用 JSON |
| `/pricelists/<id>/analytics` | 価格表 owner 向けアクセス解析画面 |

### 実行の流れ

1. `routes/scrape.py` が URL またはサイト指定からスクレイパーを選択
2. `services.scrape_queue.get_queue()` にジョブを登録
3. バックグラウンドスレッドで各サイトの `scrape_single_item()` / `scrape_search_result()` を実行
4. `filter_excluded_items()` を適用
5. JS プレビュー経路では `/api/scrape/status/<job_id>` をポーリングし、同画面へ結果を描画
6. 選択登録時のみ `/scrape/register-selected` から `save_scraped_items_to_db()` を実行
7. 非 JS / 従来経路では既存どおり待機ページ → 結果ページフローを維持

注意:

- JS プレビュー経路では抽出直後に DB 保存しない
- 非 JS の通常送信では従来どおり即時保存フローを維持している

---

## 6. 商品登録・編集フロー

### 手動追加

- `routes/main.py` の `/products/manual-add` で在庫品を直接登録できる
- 登録時に `Product`、最新 `ProductSnapshot`、デフォルト `Variant` を同時作成する
- `shop_id` は所有者チェックを行い、他ユーザーのショップは拒否する
- 画像 URL は改行または `|` 区切りで入力し、`http(s)` と `/media/...` のみ保存する
- 登録完了後は通常の `/product/<id>` 編集画面へ遷移する

### 商品編集

- `routes/products.py` の `/product/<id>` が編集画面を担当
- GET 時は最新 `ProductSnapshot` を読み、`image_urls` を分解して画像一覧へ渡す
- POST 時は `Product` 本体、`Variant` 群、SEO、英語タイトル/説明を更新する
- 画像 UI は `templates/product_detail.html` 上で SortableJS を使い、`image_urls_json` 隠しフィールドへ現在順序を保持する
- 画像順序や削除内容が変わった場合のみ、新しい `ProductSnapshot` を 1 件追加して `image_urls` を差し替える
- 既存スナップショットは破壊せず残すため、export/catalog は常に「最新スナップショット」を参照して動作する
- 公開カタログは `PriceList.layout` に応じて `grid` / `editorial` の 2 レイアウトを切り替える
- 商品カードの詳細は `catalog_product_detail` JSON を fetch してモーダル表示する
- `catalog_view` と `catalog_product_detail` では `CatalogPageView` を非同期依存なしで記録する
- owner 向けの analytics 画面では PV、推定ユニーク、デバイス比率、流入カテゴリ、人気商品、最近のアクセスを表示する

---

## 7. 定期パトロール

### Scheduler

- `app.py` で `APScheduler` を起動
- 15 分ごとに `MonitorService.check_stale_products(limit=15)` を実行
- Windows では `fcntl` が存在しないため、ローカル検証時は単一プロセス起動にフォールバック

### パトロールの実態

- `services.monitor_service._BROWSER_SITES = frozenset()`
- 各 patrol 実装は `patrol.fetch(url)` を driver なしで呼び出す
- Stage 4a / 4b 後、Yahoo / オフモール / SNKRDUNK / ヤフオク / 駿河屋の Selenium デッドコードは削除済み

---

## 8. データモデルの要点

### `products`

主要フィールド:

- `id`
- `user_id`
- `shop_id`
- `site`
- `source_url`
- `last_title`
- `last_price`
- `last_status`
- `custom_title`
- `custom_description`
- `custom_title_en`
- `custom_description_en`
- `pricing_rule_id`
- `selling_price`
- `archived`
- `deleted_at`
- `updated_at`

### `variants`

主要フィールド:

- `product_id`
- `option1_value`
- `option2_value`
- `price`
- `inventory_qty`
- `sku`
- `grams`
- `hs_code`
- `country_of_origin`

### `product_snapshots`

- `product_id`
- `scraped_at`
- `title`
- `price`
- `status`
- `description`
- `image_urls`

### `price_lists`

- `user_id`
- `name`
- `token`
- `is_active`
- `currency_rate`
- `layout`
- `notes`

### `catalog_page_views`

- `pricelist_id`
- `viewed_at`
- `ip_hash`
- `user_agent_short`
- `referrer_domain`
- `product_id`

未実装のロードマップ項目:

- なし

---

## 9. 現在の制約と未着手事項

### 完了済み

- Stage 4b: Selenium 完全削除
- Block B の主要 UI 改善
- Block C の主要機能追加
- Block D-1 の商品手動追加
- Block D-2 のカタログレイアウト切替
- Block D-3 の商品詳細モーダル
- Block D-4 のアクセス解析

### 未着手 / 一部未着手

- Block D: 完了
- ライブサイト向け自動スモークテストの整備

---

## 10. 検証の基準

Stage 4b 完了後の最低基準:

- `tests/test_stage4_selenium_removal.py` が通る
- `tests/test_scrape_queue.py` が通る
- `tests/test_rakuma_playwright.py` が通る
- `tests/test_scraping_logic.py` が通る
- `rg -n "selenium|create_driver|webdriver_manager|undetected_chromedriver"` で本体コードに残骸がない

詳細な完了記録は [`STAGE_4_RESULTS.md`](./STAGE_4_RESULTS.md) を参照。
