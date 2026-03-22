# ESP — 日本向けマーケットプレイス 商品管理・スクレイピングシステム

> 複数の日本語ECサイトから商品情報を自動収集し、Shopify / eBay への一括出品・在庫管理を支援する  
> Flask ベースの Web アプリケーションです。

---

## 目次

1. [プロジェクト概要](#1-プロジェクト概要)
2. [主要機能](#2-主要機能)
3. [対応スクレイピングサイト](#3-対応スクレイピングサイト)
4. [技術スタック](#4-技術スタック)
5. [システムアーキテクチャ](#5-システムアーキテクチャ)
6. [データベース構造](#6-データベース構造)
7. [セットアップ・起動方法](#7-セットアップ起動方法)
8. [環境変数](#8-環境変数)
9. [使い方](#9-使い方)
10. [自動監視（パトロール）](#10-自動監視パトロール)
11. [CSV エクスポート](#11-csv-エクスポート)
12. [テスト](#12-テスト)
13. [ディレクトリ構成](#13-ディレクトリ構成)
14. [開発ロードマップ・現状ステータス](#14-開発ロードマップ現状ステータス)
15. [運用上の注意事項](#15-運用上の注意事項)

---

## 1. プロジェクト概要

**ESP** は、日本国内の主要フリマ・ショッピングサイトから商品情報をスクレイピングし、  
以下の一連のワークフローを自動化・管理するツールです。

```
仕入れサイトの商品を検索・抽出
        ↓
商品データ（タイトル・価格・画像・バリエーション）を DB に保存
        ↓
価格計算ルールを適用して販売価格を自動算出
        ↓
Shopify / eBay 用 CSV を生成して一括出品
        ↓
15 分ごとのパトロールで価格変動・売り切れを自動検知
```

---

## 2. 主要機能

### スクレイピング・商品登録
- **キーワード検索スクレイピング** — サイトと検索条件（キーワード、価格帯、件数）を指定して一括取得
- **単品 URL スクレイピング** — 商品 URL を直接指定して詳細情報を取得
- **非同期キューシステム** — 複数の取得ジョブを並列処理（HTTP 最大 10 並列、ブラウザ 2 並列）
- **手動商品登録** — スクレイピングを使わずに直接登録
- **CSV インポート** — 一括商品インポート

### 商品データ管理
- タイトル・説明文・タグ・Vendor・SEO 設定の編集
- **バリエーション（カラー・サイズ等）** の CRUD および一括生成ウィザード
- **説明文テンプレート** 機能（複数テンプレートの使い回し・一括適用）
- ソフトデリート（ゴミ箱）＆アーカイブ（SOLD 管理）
- スナップショット履歴（価格・ステータスの変更履歴）

### 価格計算
- **動的価格計算**: `販売価格 = (仕入値 + 送料) × (1 + 利益率%) + 固定費`
- ユーザーごとの価格ルール CRUD
- API 経由での一括価格更新（固定額・利益率・複合の各モード）

### エクスポート
| エクスポート種別 | 形式 | 主な用途 |
|---|---|---|
| Shopify 商品登録 CSV | Shopify 標準 | 新規出品・更新 |
| Shopify 在庫更新 CSV | Handle + Qty | 在庫数同期 |
| Shopify 価格更新 CSV | Handle + Price | 価格差分更新 |
| eBay File Exchange CSV | eBay 標準 | eBay 一括出品 |

### 公開カタログ
- トークンベースの公開価格表（仕入れ先バイヤー向け）
- レイアウト選択（グリッド / エディトリアル）
- ページビュー解析（IP ハッシュ・リファラー・UA）
- 通貨変換（JPY → USD）

### ユーザー・ショップ管理
- マルチユーザー対応（Flask-Login によるセッション管理）
- ユーザーごとに複数ショップ（Shopify アカウント）を管理
- 商品・価格表はユーザーごとに完全分離

### その他
- **除外キーワードフィルター**（部分一致 / 完全一致）
- **セルフヒーリング CSS セレクター**（サイト変更時に自動修復）
- **スクレイピングメトリクス** とヘルスチェック

---

## 3. 対応スクレイピングサイト

| サイト | URL | 取得方式 | 検索 | 詳細 | パトロール |
|--------|-----|---------|------|------|------------|
| **メルカリ** | jp.mercari.com | Playwright (StealthyFetcher) | ✅ | ✅ | ✅ |
| **ラクマ** | fril.jp / item.fril.jp | Playwright（検索）+ HTTP（詳細） | ✅ | ✅ | ✅ |
| **Yahoo!ショッピング** | shopping.yahoo.co.jp | HTTP（JSON in page） | ✅ | ✅ | ✅ |
| **ヤフオク!** | auctions.yahoo.co.jp | HTTP（埋め込み JSON） | ✅ | ✅ | ✅ |
| **駿河屋** | suruga-ya.jp | HTTP（JSON-LD） | ✅ | ✅ | ✅ |
| **オフモール** | netmall.hardoff.co.jp | HTTP（JSON-LD） | ✅ | ✅ | ✅ |
| **SNKRDUNK** | snkrdunk.com | 動的フェッチ（検索）+ HTTP（詳細） | ✅ | ✅ | ✅ |

> **Selenium 不使用**: 2026-03-10 の Stage 4b 完了により、全サイトが Playwright + HTTP（Scrapling）に移行済みです。

---

## 4. 技術スタック

### Web フレームワーク
| ライブラリ | バージョン | 用途 |
|---|---|---|
| Flask | 最新 | Web フレームワーク |
| SQLAlchemy | 最新 | ORM |
| Flask-Login | 最新 | 認証・セッション |
| Flask-APScheduler | 最新 | バックグラウンドスケジューラ |
| Gunicorn | 最新 | WSGI 本番サーバー |

### スクレイピング・HTTP
| ライブラリ | バージョン | 用途 |
|---|---|---|
| Scrapling | 最新 | 高速 HTTP クライアント（Fetcher / StealthyFetcher） |
| Playwright | 最新 | ブラウザ自動操作（JS 重複サイト用） |
| Patchright | 最新 | Playwright ブラウザバイナリ管理 |
| curl_cffi | 最新 | アンチボット HTTP クライアント |
| BeautifulSoup4 | 最新 | HTML パース |
| requests | 最新 | HTTP クライアント（補助用） |

### データ処理・その他
| ライブラリ | バージョン | 用途 |
|---|---|---|
| pandas | 最新 | CSV 生成・処理 |
| msgspec | 最新 | 高速 JSON シリアライズ |
| browserforge | 最新 | ブラウザフィンガープリント生成 |

### インフラ
- **コンテナ**: Docker（Python 3.11-slim ベース）
- **DB**: SQLite（デフォルト）/ PostgreSQL・MySQL（DATABASE_URL 指定時）
- **設定**: 環境変数

---

## 5. システムアーキテクチャ

```
┌────────────────────────────────────────────────────────────────┐
│                   Flask Web アプリケーション                    │
├────────────────────────────────────────────────────────────────┤
│  ルート (13 ブループリント)                                     │
│  main / products / scrape / export / api                        │
│  auth / shops / pricing / pricelist / catalog                   │
│  archive / trash / settings / import_routes                     │
├────────────────────────────────────────────────────────────────┤
│  サービス層 (15 モジュール)                                     │
│  ┌─────────────────┐  ┌──────────────────┐                   │
│  │ scrape_queue    │  │ monitor_service   │ ← APScheduler      │
│  │ product_service │  │ pricing_service   │                    │
│  │ image_service   │  │ filter_service    │                    │
│  │ selector_healer │  │ patrol/ (×7)      │                    │
│  └─────────────────┘  └──────────────────┘                   │
├────────────────────────────────────────────────────────────────┤
│  スクレイパー層 (7 モジュール)                                  │
│  mercari / rakuma / yahoo / yahuoku                             │
│  surugaya / offmall / snkrdunk                                  │
├────────────────────────────────────────────────────────────────┤
│  スクレイピングクライアント                                     │
│  fetch_static (Scrapling Fetcher)                               │
│  fetch_dynamic (Playwright StealthyFetcher)                     │
├────────────────────────────────────────────────────────────────┤
│  外部サイト (7 日本語 EC サイト)                                │
└────────────────────────────────────────────────────────────────┘
              ↕
┌────────────────────────────────────────────────────────────────┐
│  データベース (SQLite / PostgreSQL)  ← 13 モデル               │
└────────────────────────────────────────────────────────────────┘
```

### スクレイプキューシステム

- **プロセス内インメモリキュー**（`services/scrape_queue.py`）
- ThreadPoolExecutor で HTTP（最大 10 並列）とブラウザ（最大 2 並列）を分離
- ジョブ状態: `QUEUED → RUNNING → COMPLETED / FAILED`
- ⚠️ Gunicorn は必ず `--workers 1` で運用（インメモリのため複数ワーカー不可）

---

## 6. データベース構造

| モデル | テーブル名 | 概要 |
|--------|-----------|------|
| `User` | users | ユーザーアカウント |
| `Shop` | shops | ショップ（Shopify 等）管理 |
| `Product` | products | 商品情報（スクレイプ結果） |
| `Variant` | variants | バリエーション（色・サイズ等） |
| `ProductSnapshot` | product_snapshots | 価格・ステータス変更履歴 |
| `DescriptionTemplate` | description_templates | 説明文テンプレート |
| `PricingRule` | pricing_rules | 価格計算ルール |
| `ExclusionKeyword` | exclusion_keywords | 除外キーワードフィルター |
| `PriceList` | price_lists | 公開価格表 |
| `PriceListItem` | price_list_items | 価格表に含まれる商品 |
| `CatalogPageView` | catalog_page_views | カタログ閲覧解析ログ |

主要カラム（`products` テーブル抜粋）:

| カラム | 型 | 説明 |
|--------|----|------|
| `site` | String | 取得元サイト名 |
| `source_url` | String | 元商品 URL |
| `last_title` | String | 最新タイトル |
| `last_price` | Integer | 最新価格（JPY） |
| `last_status` | String | ステータス（on_sale / sold / deleted） |
| `custom_title` | String | カスタムタイトル（日本語） |
| `custom_title_en` | String | カスタムタイトル（英語） |
| `selling_price` | Float | 算出済み販売価格 |
| `patrol_fail_count` | Integer | パトロール連続失敗回数 |

---

## 7. セットアップ・起動方法

### Docker を使う場合（推奨）

```bash
# イメージのビルド
docker build -t esp-app .

# 起動（ポート 10000）
docker run -p 10000:10000 \
  -e SECRET_KEY=your-secret-key \
  -e DATABASE_URL=sqlite:///mercari.db \
  esp-app
```

### ローカル環境（Python 3.11+）

```bash
# 1. 依存パッケージのインストール
pip install -r requirements.txt

# 2. Playwright / Scrapling ブラウザのインストール
scrapling install
patchright install chromium

# 3. 管理者ユーザーの作成
flask add-user

# 4. 開発サーバー起動
flask run --port 5000

# 本番相当の起動（シングルワーカー必須）
gunicorn --worker-class gthread --workers 1 --threads 8 \
         --max-requests 0 --timeout 600 \
         --bind 0.0.0.0:5000 wsgi:app
```

---

## 8. 環境変数

| 変数名 | デフォルト | 説明 |
|--------|----------|------|
| `SECRET_KEY` | なし（必須） | Flask セッション署名キー |
| `DATABASE_URL` | `sqlite:///mercari.db` | DB 接続文字列 |
| `PORT` | `10000` | Gunicorn バインドポート |
| `IMAGE_STORAGE_PATH` | `static/images` | ダウンロード画像の保存先 |
| `MERCARI_USE_NETWORK_PAYLOAD` | `false` | メルカリ API インターセプト有効化 |
| `{SITE}_DETAIL_CONCURRENCY` | サイト依存 | 詳細ページの並列取得数 |
| `{SITE}_DETAIL_TIMEOUT` | サイト依存 | タイムアウト秒数 |
| `{SITE}_DETAIL_RETRIES` | サイト依存 | リトライ回数 |
| `{SITE}_DETAIL_BACKOFF` | サイト依存 | リトライ間隔（秒） |

`{SITE}` には `MERCARI`, `RAKUMA`, `YAHOO`, `YAHUOKU`, `SURUGAYA`, `OFFMALL`, `SNKRDUNK` が入ります。

---

## 9. 使い方

### 商品のスクレイピング

1. ブラウザで `http://localhost:5000/scrape` を開く
2. サイト（例: メルカリ）・キーワード・価格帯・件数を入力して「実行」
3. ジョブがキューに投入され、ステータス画面でリアルタイム確認
4. 結果プレビュー画面で取り込む商品にチェックを入れて「登録」

単品 URL を直接入力してスクレイピングすることも可能です。

### 商品の編集・価格設定

1. 商品一覧（`/`）から商品を選択
2. タイトル・説明文・バリエーション・価格ルールを編集
3. 販売価格は `(仕入値 + 送料) × (1 + 利益率%) + 固定費` で自動計算

### エクスポート

1. `/export/shopify`（商品登録 CSV）、`/export_stock_update`（在庫更新 CSV）等にアクセス
2. CSV をダウンロードして Shopify 管理画面からインポート

### 公開カタログの作成

1. `/pricelist` でカタログを作成
2. 商品を追加・並び替え・価格カスタマイズ
3. 発行されたトークン URL（`/catalog/<token>`）をバイヤーへ共有

---

## 10. 自動監視（パトロール）

APScheduler により **15 分おきに全登録商品を巡回**し、価格・ステータスの変化を検出します。

- **軽量パトロール** — 詳細再スクレイピングではなく価格・在庫のみ取得
- **指数バックオフ** — 連続失敗時は次回間隔を延長（最大 180 分）
- **自動アーカイブ** — SOLD / DELETED 検知時にステータスを自動更新
- **スナップショット保存** — 変化があった場合に `ProductSnapshot` へ記録

---

## 11. CSV エクスポート

### Shopify 商品登録 CSV（`/export/shopify`）

| 列名 | 内容 |
|------|------|
| Handle | URL ハンドル（SKU ベース） |
| Title | カスタムタイトル |
| Body (HTML) | 商品説明 |
| Vendor / Type / Tags | 商品分類 |
| Variant Price | 算出済み販売価格 |
| Variant SKU | SKU |
| Image Src | 自社サーバー経由の画像 URL |
| Status | active / draft |

### eBay File Exchange CSV（`/export_ebay`）

- 為替レート（JPY → USD）と利益率を適用した価格を出力
- Item Specifics フィールド対応

---

## 12. テスト

```bash
# テスト全体を実行
python -m pytest tests/ -v

# 特定のテストファイルのみ
python -m pytest tests/test_scrape_queue.py -v

# キーワードで絞り込み
python -m pytest -k "mercari" -v
```

主なテストファイル:

| ファイル | 内容 |
|---------|------|
| `test_scrape_queue.py` | キューのジョブライフサイクル |
| `test_rakuma_playwright.py` | ラクマスクレイパー |
| `test_mercari_*.py` | メルカリスクレイパー全般 |
| `test_stage4_selenium_removal.py` | Selenium 完全削除の確認 |
| `test_e2e_routes.py` | 全ルートの E2E テスト |
| `test_monitor_service.py` | パトロールサービス |
| `test_selector_healer.py` | セルフヒーリングセレクター |
| `test_auth.py` | 認証 |

> **既知の前提**: CI 環境では `scrapling` の一部依存（`patchright`, `msgspec`）が未インストールの場合があります。  
> テストは `sys.modules` モックで対応しています（`tests/conftest.py` 参照）。

---

## 13. ディレクトリ構成

```
ESP/
├── app.py                      # Flask アプリ本体、ブループリント登録、スケジューラ起動
├── models.py                   # SQLAlchemy ORM モデル（13 テーブル）
├── database.py                 # DB 設定（SQLite WAL モード、SessionLocal）
├── requirements.txt            # Python 依存パッケージ
├── Dockerfile                  # Docker ビルド設定
├── selector_config.py          # CSS セレクター読み込み・キャッシュ
├── utils.py                    # 共通ユーティリティ
│
├── *_db.py                     # サイト別スクレイパー（7 ファイル）
│   ├── mercari_db.py
│   ├── rakuma_db.py
│   ├── yahoo_db.py
│   ├── yahuoku_db.py
│   ├── surugaya_db.py
│   ├── offmall_db.py
│   └── snkrdunk_db.py
│
├── routes/                     # Flask ブループリント（13 モジュール）
│   ├── main.py                 # ダッシュボード、商品一覧
│   ├── scrape.py               # スクレイピングフォーム、キュー
│   ├── export.py               # CSV エクスポート
│   ├── products.py             # 商品詳細・編集
│   ├── api.py                  # JSON API
│   ├── auth.py                 # 認証
│   ├── shops.py                # ショップ管理
│   ├── pricing.py              # 価格ルール
│   ├── pricelist.py            # 価格表管理
│   ├── catalog.py              # 公開カタログ
│   ├── archive.py              # アーカイブ管理
│   ├── trash.py                # ゴミ箱管理
│   ├── settings.py             # ユーザー設定
│   └── import_routes.py        # CSV インポート
│
├── services/                   # ビジネスロジック層（15 モジュール）
│   ├── scrape_queue.py         # ジョブキューシステム
│   ├── scraping_client.py      # fetch_static / fetch_dynamic ラッパー
│   ├── monitor_service.py      # 定期パトロールサービス
│   ├── product_service.py      # 商品 DB 永続化
│   ├── pricing_service.py      # 価格計算
│   ├── filter_service.py       # キーワードフィルター
│   ├── image_service.py        # 画像ダウンロード・配信
│   ├── selector_healer.py      # CSS セレクター自動修復
│   ├── mercari_item_parser.py  # メルカリ DOM パーサー
│   ├── rakuma_item_parser.py   # ラクマ DOM パーサー
│   └── patrol/                 # 軽量パトロールスクレイパー（7 サイト分）
│
├── templates/                  # Jinja2 テンプレート（20 ファイル）
├── static/                     # CSS / JS / 画像アセット
├── config/                     # CSS セレクター設定・フィンガープリントキャッシュ
├── tests/                      # pytest テストスイート（18 ファイル）
├── docs/                       # 設計書・ロードマップ・仕様書
└── knowledge/                  # 運用ナレッジベース（インシデント記録等）
```

---

## 14. 開発ロードマップ・現状ステータス

### 完了済みマイルストーン

| Stage | 内容 | 完了日 |
|-------|------|--------|
| Stage 0 | キューシステム構築（ThreadPoolExecutor） | 2026-03 |
| Stage 1 | ラクマ Playwright 移行 | 2026-03 |
| Stage 2 | メルカリパトロール Playwright 移行 | 2026-03 |
| Stage 3 | メルカリ全体 Playwright 移行 | 2026-03 |
| Stage 4a | パトロール層 Selenium 完全削除 | 2026-03 |
| Stage 4b | DB スクレイピング層 Selenium 完全削除 | 2026-03-10 |

### 現在の技術状態

- ✅ Selenium ゼロ（全サイト Playwright + HTTP に移行済み）
- ✅ Docker イメージから Chrome 導入処理を削除済み
- ✅ 7 サイトスクレイパー + 7 軽量パトロールスクレイパー稼働中
- ✅ セルフヒーリング CSS セレクターシステム実装済み（ベータ）
- ✅ マルチユーザー・マルチショップ対応

### 今後の予定課題

- UI 改善（商品一覧・編集・抽出ページのコンパクト化）
- 価格表管理ページの強化
- ジョブキューの DB 永続化（複数ワーカー対応化）

詳細は [`docs/UNIFIED_ROADMAP.md`](docs/UNIFIED_ROADMAP.md) を参照してください。

---

## 15. 運用上の注意事項

### ⚠️ Gunicorn ワーカー数は必ず 1 にする

```bash
gunicorn --workers 1 --threads 8 --max-requests 0 ...
```

スクレイプキューはプロセス内インメモリシングルトンです。  
`--workers` を 2 以上にするとジョブ状態が別プロセスから参照できなくなります。

### `--max-requests 0` を必ず指定する

`max-requests > 0` に設定するとワーカーが定期再起動し、実行中のバックグラウンドジョブが失われます。

### Playwright ブラウザキャッシュ

Docker 環境では `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` を設定しており、  
root ユーザーと実行ユーザー（myuser）で Playwright ブラウザを共有しています。

### データベース

- デフォルトは SQLite（`mercari.db`）で WAL モードが有効です
- 本番環境では `DATABASE_URL` 環境変数で PostgreSQL / MySQL を指定することを推奨します

---

## ライセンス

このプロジェクトのライセンス条件については、リポジトリオーナーにお問い合わせください。
