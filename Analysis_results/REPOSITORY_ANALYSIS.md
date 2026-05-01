# ESP リポジトリ解析レポート

> **作成日:** 2026-05-01
> **リポジトリ:** halc8312/ESP
> **対象範囲:** コードベース全体（ルートからサブディレクトリまで）

---

## 1. ディレクトリ構成

```
ESP/
├── app.py                      # Flaskアプリケーションファクトリ / ブートストラップ
├── models.py                   # SQLAlchemy ORMモデル（13テーブル）
├── database.py                 # DB設定、マイグレーション、スキーマ管理
├── cli.py                      # Flask CLIコマンド（デプロイ検証、ローカルテスト等）
├── worker.py                   # 専用RQワーカーエントリーポイント
├── wsgi.py                     # Gunicorn WSGIエントリーポイント
├── requirements.txt            # Python依存関係
├── Dockerfile                  # コンテナイメージ定義
├── docker-compose.local.yml    # ローカル開発用スタック（PostgreSQL + Redis）
├── render.yaml                 # Renderデプロイトポロジ（分割アーキテクチャ）
├── alembic.ini                 # Alembicマイグレーション設定
├── alembic/                    # マイグレーションスクリプト（7バージョン）
│   └── versions/
├── routes/                     # Flaskブループリント（13ルートモジュール）
├── services/                   # ビジネスロジック層（30+サービスモジュール）
├── templates/                  # Jinja2 HTMLテンプレート（20ファイル）
├── static/                     # 静的アセット
│   ├── css/                    # スタイルシート（catalog.css, style.css）
│   ├── js/                     # JavaScript（scrape_form.js, app_ui.js等）
│   └── images/                 # 画像ストレージ
├── tests/                      # Pytestテストスイート（77テストファイル）
│   ├── conftest.py             # テストフィクスチャと設定
│   └── fixtures/               # テストデータ（HTMLフィクスチャ等）
├── docs/                       # ドキュメント（Runbook、仕様、アーキテクチャ）
├── config/                     # 設定ファイル
│   ├── scraping_selectors.json # スクレイパー用CSSセレクタ
│   ├── element_fingerprints.json
│   └── heal_history.jsonl
├── jobs/                       # ワーカータスク定義
│   ├── scrape_tasks.py
│   ├── translation_tasks.py
│   └── bg_removal_tasks.py
├── services/translator/        # 翻訳バックエンド（OpenAI、Argos）
├── services/bg_remover/        # 背景除去バックエンド（rembg）
├── services/patrol/            # 軽量パトロールスクレイパー（7サイト）
├── knowledge/                  # オペレーション知識ベース
├── scripts/                    # ユーティリティスクリプト
└── llama.cpp/                  # LLM統合（バンドル済み、注意して触ること）
```

---

## 2. 主要ファイル一覧

### コアアプリケーションファイル

| ファイル | 行数 | 役割 |
|---------|------|------|
| `app.py` | 584 | Flaskアプリケーションファクトリ、ブループリント登録、スケジューラ設定、セキュリティヘッダ、HTTPS強制、CLI登録 |
| `models.py` | 460 | 13個のSQLAlchemy ORMモデル（データベーススキーマ定義） |
| `database.py` | 386 | DBエンジン作成、セッション管理、スキーマブートストラップ、加算マイグレーション、ドリフト検出 |
| `cli.py` | 3,624 | 豊富なFlask CLIコマンド（デプロイ検証、ローカルテスト、スキーマ管理） |
| `worker.py` | 62 | 専用RQワーカーエントリーポイント（ブラウザプール初期化、セレクタ修復） |
| `wsgi.py` | - | Gunicorn用WSGIエントリーポイント |

### サービス層（services/）主要ファイル（行数順）

| ファイル | 行数 | 役割 |
|---------|------|------|
| `selector_healer.py` | 1,016 | 自己修復CSSセレクタエンジン（フィンガープリントベース探索） |
| `mercari_item_parser.py` | 1,292 | 複雑なMercari DOMパーサー（ネットワークペイロード抽出） |
| `scrape_job_store.py` | 569 | ジョブ永続化、状態管理、除外追跡 |
| `repair_store.py` | 539 | セレクタ修復候補管理と活性化 |
| `queue_backend.py` | 488 | 抽象キューインターフェース（インメモリ + RQ対応） |
| `product_service.py` | 340 | プロダクトCRUD、スナップショット管理、画像処理 |
| `worker_runtime.py` | 299 | ワーカー起動、ヘルスチェック、バックログ診断 |
| `browser_pool.py` | 255 | 共有Playwrightブラウザプール（リサイクル機能） |
| `monitor_service.py` | 285 | 軽量パトロールサービス（15分間隔） |
| `pricing_service.py` | 186 | 動的価格計算エンジン |
| `image_service.py` | 139 | 画像ダウンロード、検証、キャッシュ |
| `scraping_client.py` | 251 | Scrapling/Playwright HTTPクライアントラッパー |

### データベーススクレイパー（ルートレベル）

| ファイル | 行数 | 対象サイト | 手法 |
|---------|------|-----------|------|
| `mercari_db.py` | 1,234 | Mercari | ブラウザ + ネットワークペイロード |
| `snkrdunk_db.py` | 18,637 | SNKRDUNK | 動的フェッチ + HTTP詳細 |
| `surugaya_db.py` | 42,742 | Surugaya | HTTP JSON-LD |
| `yahoo_db.py` | 18,637 | Yahoo Shopping | HTTP（ページ内JSON） |
| `yahuoku_db.py` | 12,985 | Yahoo Auctions | HTTP（埋め込みJSON） |
| `offmall_db.py` | 13,405 | Offmall | HTTP JSON-LD |
| `rakuma_db.py` | 7,693 | Rakuma | Playwright検索 + HTTP詳細 |

---

## 3. アプリの起動方法

### 本番環境（Render分割デプロイ）

**環境変数（必須）:**
```bash
SECRET_KEY=（32文字以上、webとworkerで同一）
DATABASE_URL=（Render自動提供）
REDIS_URL=（Render自動提供）
OPENAI_API_KEY=（翻訳使用時）
BG_REMOVAL_INTERNAL_SECRET=（HMAC認証用）
```

**サービス別起動:**

**esp-web（Flaskアプリ）:**
- コマンド: `gunicorn --worker-class gthread --workers 1 --threads 8 --max-requests 0 --timeout 600 --bind 0.0.0.0:${PORT:-10000} --bind 0.0.0.0:${INTERNAL_PORT:-8080} wsgi:app`
- 環境変数: `APP_ENV=production`, `RUNTIME_ROLE=web`, `SCRAPE_QUEUE_BACKEND=rq`, `WEB_SCHEDULER_MODE=disabled`
- ポート: 10000（公開）, 8080（内部）

**esp-worker（バックグラウンドワーカー）:**
- コマンド: `python worker.py`
- 環境変数: `RUNTIME_ROLE=worker`, `WORKER_ENABLE_SCHEDULER=1`, `WARM_BROWSER_POOL=1`
- ブラウザプール事前温め: `BROWSER_POOL_WARM_SITES=mercari`
- バックログ診断: `WORKER_BACKLOG_WARN_COUNT=25`, `WORKER_BACKLOG_WARN_AGE_SECONDS=900`

**esp-postgres / esp-keyvalue:**
- Renderマネージドサービス（自動プロビジョニング）

### ローカル開発環境

```bash
# 1. Docker ComposeでDB+Redis起動
docker compose -f docker-compose.local.yml up -d

# 2. Python依存関係インストール
pip install -r requirements.txt

# 3. ブラウザインストール
scrapling install
patchright install chromium

# 4. データベース初期化
flask create-user
flask db-smoke --require-backend postgresql --apply-migrations

# 5. Webサーバー起動（別ターミナルでワーカーも）
flask run --port 5000

# 6. ワーカー起動（別ターミナル）
python worker.py
```

### キーポイント
- **Gunicorn workersは常に1個**（インメモリキュー+共有ブラウザプールの制約）
- 本番では`SCRAPE_QUEUE_BACKEND=rq`、開発では`inmemory`がデフォルト
- ワーカーは`WEB_INTERNAL_HOST`経由でwebサービスにHMACで通信

---

## 4. 主要機能

### プロダクト調達・スクレイピング
- **7サイト対応**: Mercari, Rakuma, Yahoo Shopping, Yahoo Auctions, Surugaya, Offmall, SNKRDUNK
- **2モード:**
  - キーワード検索 → 一括取得
  - 単体URL → 即時詳細取得
- **プレビューモード** → DB投入前確認
- **非同期キュー** → リアルタイムステータス監視
- **セレクタ自己修復** → DOM変更自動検知、代替セレクタ発見、設定永続化
- **除外キーワード** → ユーザー定義フィルタ（部分一致/完全一致）
- **価格フィルタ** → スクレイピング時最小/最大価格適用

### プロダクトデータ管理
- **リッチテキストエディタ** → 説明文HTML構造保持・サニタイズ
- **多言語対応** → 日本語原文 + 英語翻訳フィールド
- **画像処理:**
  - スクレイピed画像ローカルキャッシュ（`/media/`経由）
  - オペレータアップロード（PNG/JPG/GIF/WEBP）
  - 画像URL追加・並び替え・削除
  - 背景除去（rembg、キューイング）
- **バリエーション管理** → 色/サイズ別在庫
- **説明テンプレート** → 再利用可能リッチテキスト
- **ソフトデリート** → ゴミ箱（30日自動削除）
- **アーカイブ** → SOLD商品別ビュー
- **スナップショット** → 価格/状態履歴（パトロール毎に記録）

### 価格計算・エクスポート
- **動的価格エンジン:**
  ```
  売価 = (仕入 + 送料) × (1 + マージン%) + 固定費
  ```
  - ユーザー別デフォルト価格ルール
  - プロダクト個別オーバーライド対応
  - パトロール更新時に自動再計算
- **CSVエクスポート:**
  - **Shopify** - フル商品登録CSV（全Shopify列: Title, Body, Vendor, Type, Tags, Variants, Images, SEO, Google Shopping等）
  - **Shopify在庫更新** - Handle + 在庫数
  - **Shopify価格更新** - Handle + バリアント価格
  - **eBay File Exchange** - PayPal email、支払い/返品/配送プロファイル、通貨換算（JPY→USD）含む

### 翻訳
- **AI翻訳**（OpenAI GPT-4.1-nano または Argosオフライン）
- **範囲:** タイトルのみ、説明のみ、または両方
- **HTML構造保持** → テキストノード分割、タグ保持
- **ソースハッシュ追跡** → 翻訳適用後に日本語原文変更を検知
- **提案ワークフロー** → キュー → ポーリング → 適用/拒否

### 背景除去
- **ワンクリック除去**（rembg u2netpモデル）
- **非同期ジョブ** → メディアワーカーへキューイング
- **適用前プレビュー**
- **HMAC認証アップロード**（ワーカー→Webサービス）
- **自動ダウンスケーリング** → 最大2000px（メモリ制御）

### 公開カタログ（顧客向け）
- **トークンベースアクセス** → ログイン不要、共有可能URL
- **3レイアウト:** Grid, Editorial, List
- **2テーマ:** Dark / Light（カタログ毎）
- **ショップブランディング** → ロゴ・ショップ名表示
- **検索** → プロダクト全文検索
- **クイックビュー** → モーダルプレビュー
- **通貨換算** → JPY→USD（カタログ設定レート）
- **アナリティクス** → ページビュー記録（IPハッシュ、リファラ、UA、プロダクト）
- **セキュリティ:** `source_url`、`site`、内部調達情報を露出しない

### ユーザー・ショップ管理
- **マルチユーザー** → ユーザー毎データ完全分離
- **マルチショップ** → 1ユーザーが複数Shopify/eBayストア管理可能
- **セッション based ショップ選択** → UIで現在のショップ切替
- **レート制限** → Redisバックエンド（ログイン5/15min、登録3/hour）

### 自動監視（パトロール）
- **15分間隔**（APScheduler）
- **軽量フェッチャー** → 非MercariサイトはHTTPのみ（Chromeなし）
- **Mercari** → 共有ブラウザプール（Playwright）使用
- **指数バックオフ** → 失敗時（最大180分）
- **自動アーカイブ** → SOLD/DELETED状態変化検出
- **価格変更検出** → 価格ルール存在時`sellin_price`自動更新
- **ソフトセールドヒステリシス**（Mercari） → 2連続"sold"信号が必要（誤検知防止）

### 自己修復セレクタ
- **フィンガープリントベース要素識別** → 小幅DOM変更に耐性
- **候補生成** → 複数セレクタ戦略（CSS、XPath代替）
- **スコアリング** → ヒューリスティック一致品質
- **カナリー検証** → 提案セレクタを既知URLでテスト→活性化
- **Webhook通知** → Discordへ修復候補アラート（オプション）
- **手動レビュー経路** → 候補→活性化または拒否

---

## 5. データベース / モデル構成

### 13個のテーブル（models.py）

#### コアドメインモデル

| モデル | テーブル | 主なフィールド | リレーション |
|--------|---------|---------------|-------------|
| User | `users` | username, password_hash | shops, products, pricing_rules, exclusion_keywords, description_templates, price_lists, scrape_jobs, translation_suggestions, image_processing_jobs |
| Shop | `shops` | user_id, name, logo_url | products, user, price_lists |
| Product | `products` | user_id, site, shop_id, source_url, last_title, last_price, last_status, custom_title, custom_description, custom_title_en, custom_description_en, custom_vendor, custom_handle, tags, seo_title, seo_description, option1/2/3_name, pricing_rule_id, selling_price, manual_margin_rate, manual_shipping_cost, archived, deleted_at, patrol_fail_count | shop, user, variants, snapshots, price_list_items |
| Variant | `variants` | product_id, option1/2/3_value, sku, price, inventory_qty, grams, taxable, country_of_origin, hs_code, position | product |
| ProductSnapshot | `product_snapshots` | product_id, scraped_at, title, price, status, description, image_urls | product |

#### サポートモデル

| モデル | テーブル | 役割 |
|--------|---------|------|
| DescriptionTemplate | `description_templates` | 再利用可能説明テンプレート（ユーザー毎） |
| PricingRule | `pricing_rules` | 自動価格計算ルール（マージン%、送料、固定費） |
| ExclusionKeyword | `exclusion_keywords` | 不要商品フィルタ（部分/完全一致） |

#### カタログ・公開アクセス

| モデル | テーブル | 主なフィールド | リレーション |
|--------|---------|---------------|-------------|
| PriceList | `price_lists` | user_id, shop_id, name, token(UUID), is_active, currency_rate(JPY→USD), layout, theme, notes | items, page_views, user, shop |
| PriceListItem | `price_list_items` | price_list_id, product_id, visible, custom_price, sort_order | price_list, product |
| CatalogPageView | `catalog_page_views` | pricelist_id, viewed_at, ip_hash, user_agent_short, referrer_domain, product_id |  |

#### スクレイピング・ジョブ

| モデル | テーブル | 主なフィールド | リレーション |
|--------|---------|---------------|-------------|
| ScrapeJob | `scrape_jobs` | job_id, logical_job_id, parent_job_id, status, site, mode, requested_by, request_payload, context_payload, progress_current, progress_total, result_summary, result_payload, error_message, tracker_dismissed_at, started_at, finished_at | user, parent, events |
| ScrapeJobEvent | `scrape_job_events` | id, job_id, event_type, payload | job |

#### セレクタ修復・翻訳・画像処理

| モデル | テーブル | 主なフィールド | リレーション |
|--------|---------|---------------|-------------|
| SelectorRepairCandidate | `selector_repair_candidates` | site, page_type, field, parser, proposed_selector, source_selector, score, page_state, status | source_candidate |
| SelectorActiveRuleSet | `selector_active_rule_sets` | site, page_type, field, version, selectors_payload, is_active, source_candidate_id | source_candidate |
| TranslationSuggestion | `translation_suggestions` | product_id, user_id, scope(title/description/full), provider(argos/deepl/openai), source_hash, translated_title, translated_description, status(queued/running/succeeded/failed/applied/rejected) | product, user |
| ImageProcessingJob | `image_processing_jobs` | product_id, user_id, operation(bg_remove), provider(rembg), source_image_url, result_image_url, status | product, user |

### データベース設定
- **本番:** PostgreSQL（Render `esp-postgres`サービス）
- **開発:** SQLite（WALモード）
- **マイグレーション:** Alembic（7バージョン、加算のみ）
- **セッション管理:** SQLAlchemy 2.0（async/await対応）

---

## 6. 外部サービス連携

### APIベースサービス

| サービス | 目的 | 設定変数 | エンドポイント/ライブラリ |
|----------|------|---------|------------------------|
| **OpenAI API** | プロダクト翻訳（ja→en, GPT-4.1-nano） | `OPENAI_API_KEY`（必須）, `OPENAI_TRANSLATOR_MODEL` | `services/translator/openai_backend.py` |
| **Argos Translate** | オフライン翻訳フォールバック | - | `services/translator/argos_backend.py`（Dockerビルド時モデルプレロード） |

### 画像処理

| サービス | 目的 | 設定変数 | 備考 |
|----------|------|---------|------|
| **rembg** | 背景除去（u2netpモデル） | `BG_REMOVAL_BACKEND`（default: rembg）, `BG_REMOVAL_MODEL`（default: u2netp） | モデルはDockerビルド時プレロード、入力最大2000pxにダウンスケール |

### スクレイピング対象（7日本ECサイト）

| サイト | URL | 手法 | ブラウザプール利用 |
|--------|-----|------|-------------------|
| Mercari | jp.mercari.com | Playwright + ネットワークペイロード | ✓（詳細 + パトロール） |
| Rakuma | fril.jp / item.fril.jp | Playwright（検索） + HTTP（詳細） | ✓（検索のみ） |
| Yahoo Shopping | shopping.yahoo.co.jp | HTTP（JSONページ内） | - |
| Yahoo Auctions | auctions.yahoo.co.jp | HTTP（埋め込みJSON） | - |
| Surugaya | suruga-ya.jp | HTTP（JSON-LD） | - |
| Offmall | netmall.hardoff.co.jp | HTTP（JSON-LD） | - |
| SNKRDUNK | snkrdunk.com | 動的フェッチ + HTTP詳細フォールバック | ✓（動的 + 詳細） |

> **2026-03-10（Stage 4b）時点でSelenium完全移行済み → Playwright + Scrapling**

### インフラサービス

| サービス | 用途 | Renderサービス名 |
|----------|------|----------------|
| **PostgreSQL** | プライマリDB | `esp-postgres` |
| **Redis / Valkey** | RQキュー + レート制限ストア | `esp-keyvalue` |
| **RQ (Redis Queue)** | バックグラウンドジョブ処理 | - |
| **APScheduler** | 内蔵スケジューラ（パトロール15min、ゴミ purge 毎日3時） | - |

---

## 7. テスト構成

### テストフレームワーク
- **Pytest** 9.0.3（Flask拡張、非同期対応、モック）
- **テスト数:** 77ファイル
- **場所:** `/tests/`
- **フィクスチャ:** `/tests/fixtures/`（HTMLダンプ、テストデータ）

### 主要テストファイル

| テストファイル | サイズ | 対象範囲 |
|---------------|--------|---------|
| `test_e2e_routes.py` | 83,958 bytes | フルスタックルート統合テスト |
| `test_worker_runtime.py` | 25,842 bytes | ワーカー起動、ヘルス、バックログ |
| `test_worker_entrypoint.py` | 2,813 bytes | エントリーポイント統合 |
| `test_selector_healer.py` | 22,013 bytes | 自己修復セレクタエンジン |
| `test_bg_removal_*.py` | 19,472 bytes | 背景除去API |
| `test_mercari_*.py` | - | Mercariスクレイパー/パーサー（ネットワークペイロード、パトロール、 sold ヒステリシス） |
| `test_scrape_queue.py` | - | インメモリキューライフサイクル |
| `test_browser_pool.py` | - | 共有ブラウザプール |
| `test_monitor_service.py` | - | パトロールサービス |
| `test_product_service.py` | - | プロダクトCRUD+スナップショット |
| `test_pricing_service.py` | - | 価格計算 |
| `test_translation_*.py` | - | 翻訳API + OpenAIバックエンド |
| `test_cli_*.py` | 30+ファイル | 全CLIコマンド検証（デプロイゲート、事前チェック、Renderカットオーバー） |
| `test_database_*.py` | - | DBブートストラップ、マイグレーション、ドリフトチェック |

### テスト設定（`tests/conftest.py`）
- 自動環境リセット
- テストごとに分離SQLite DB作成（`sqlite:///test_*.db`）
- インメモリキューシングルトンリセット
- Scrapling/PlaywrightテストはHTMLフィクスチャ使用（ライブネットワーク回避）

### 推奨検証コマンド
```bash
# UI/ルート変更時
pytest tests/test_e2e_routes.py -q

# ワーカー/ランタイム変更時
pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q

# レガシー単一Web互換性ゲート
flask single-web-redeploy-readiness

# 現在の分割Render安全性ゲート
flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict
```

---

## 8. デプロイ構成

### Render分割トポロジ（本番）

#### 1. `esp-web`（Webサービス）
| 項目 | 設定 |
|------|------|
| プラン | Starter |
| 自動デプロイ | オフ |
| ヘルスチェック | `/healthz` |
| ポート | 10000（公開）, 8080（内部、ワーカー⇔Web通信用） |
| 環境変数 | `APP_ENV=production`, `RUNTIME_ROLE=web`, `SCRAPE_QUEUE_BACKEND=rq`, `WEB_SCHEDULER_MODE=disabled` |
| DB/Redis | Renderマネージド（`DATABASE_URL`, `REDIS_URL`自動提供） |
| ディスク | 10GB永続（`/var/data`）→ 画像保存 |
| シークレット | `SECRET_KEY`, `OPENAI_API_KEY`, `BG_REMOVAL_INTERNAL_SECRET`, `SELECTOR_ALERT_WEBHOOK_URL`（sync: false = 手動） |

#### 2. `esp-worker`（バックグラウンドワーカー）
| 項目 | 設定 |
|------|------|
| プラン | Standard |
| コマンド | `python worker.py` |
| 環境変数 | `RUNTIME_ROLE=worker`, `WORKER_ENABLE_SCHEDULER=1`, `WARM_BROWSER_POOL=1` |
| サイト別ブラウザプール事前温め | `BROWSER_POOL_WARM_SITES=mercari` |
| ブラウザプールフラグ | `MERCARI_USE_BROWSER_POOL_DETAIL=1`, `MERCARI_PATROL_USE_BROWSER_POOL=1`, `SNKRDUNK_USE_BROWSER_POOL_DYNAMIC=1` |
| バックログ診断 | `WORKER_BACKLOG_WARN_COUNT=25`, `WORKER_BACKLOG_WARN_AGE_SECONDS=900` |
| セレクタ修復 | `WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP=0`, `WORKER_SELECTOR_REPAIR_LIMIT=1` |
| 内部ホスト | `WEB_INTERNAL_HOST`（esp-webのプライベートホスト名自動解決）, `WEB_INTERNAL_PORT=8080` |
| シークレット | webと同じ（`SECRET_KEY`, `OPENAI_API_KEY`等） |

#### 3. `esp-keyvalue`（Redis/Valkey）
| 項目 | 設定 |
|------|------|
| プラン | Starter |
| 最大メモリポリシー | `noeviction` |
| 用途 | RQキュー + レート制限 |

#### 4. `esp-postgres`（PostgreSQL）
| 項目 | 設定 |
|------|------|
| プラン | basic-1gb |
| DB名 | `esp_production` |
| ユーザー | `esp_user` |

### Docker設定

```dockerfile
# ベースイメージ: python:3.11-slim
# システム依存: curl, libxss1, fonts-liberation, libasound2, libnspr4, libnss3, libx11-xcb1, xdg-utils, libgbm1, ca-certificates
# Python依存: requirements.txtからpipインストール
# ブラウザインストール: scrapling install + patchright install chromium（共有パス /opt/ms-playwright）
# モデルプレロード: Argos Translate (ja→en) + rembg u2netp（ビルド時ベイク）
# ユーザー: 非ルート myuser (UID 1000)
# CMD: gunicorn gthread --workers 1 --threads 8 --max-requests 0 --timeout 600 --bind 0.0.0.0:${PORT:-10000} --bind 0.0.0.0:${INTERNAL_PORT:-8080} wsgi:app
```

**重要制約:**
- Gunicorn workers = **1固定**（インメモリキューシングルトン + 共有ブラウザプール）
- `--max-requests 0` → 自動再起動無効（ワーカー状態保持のため）

---

## 9. 改善提案

### 高優先度（セキュリティ・安定性）
1. **複数ワーカー対応の本番検証**
   - RQモードでは理論上複数ワーカー可能だが、共有ブラウザプールの競合解消が必要
   - 現状`workers=1`固定、スケールアウト時にボトルネックになる可能性

2. **セレクタ修復自動適用の安全強化**
   - 現在は候補生成→手動活性化。自動適用はリスク高め
   - カナリー検証の信頼性向上（複数URL検証、AIベーススコア補完）

3. **健康チェックの詳細化**
   - `/healthz`は簡易。ワーカーバックログ、キュー長、ブラウザプール状態を包含する詳細エンドポイントを追加

### 中優先度（機能・UX）
4. **翻訳ワークフローの自動化**
   - 現在は提案→手動適用。自動適用モード（ユーザー設定）をオプションで
   - 翻訳バージョン管理（元日本語更新時の再翻訳トリガー）

5. **カタログカテゴリフィルタ**
   - 仕様は未実装。カタログ画面でカテゴリ（タグorタイプ）による絞り込みを

6. **PayPal/簡易EC連携**
   - 現在はエクスポートのみ。支払いリンク生成や在庫同期を将来的に

7. **画像背景除去の進化**
   - 現在rembg単一モデル。AIモデル選択（u2net, isnet-general-use等）やクラウドAPIオプションを
   - バッチ一括処理モード

### 低優先度（パフォーマンス・ops）
8. **画像キャッシュの永続化**
   - 現在はローカルディスク（`/var/data`）。S3等クラウドストレージ対応で冗長化

9. **パトロールのサイト別間隔調整**
   - 現在15分固定。サイトの更新頻度に応じて柔軟に設定可能に

10. **スクレイパー監視ダッシュボード**
    - セレクタ修復候補、パトロール失敗率、ジョブログを可視化する管理画面

11. ** Alembic ロールバック対応**
    - 現在は加算マイグレーションのみ。ダウングレードスクリプトを整備

### 技術的負債
12. **`cli.py` 3,624行の分解**
    - 1ファイル过大。コマンド群を`cli/`ディレクトリに分割再構成

13. **`snkrdunk_db.py` 18,637行、`surugaya_db.py` 42,742行の分割**
    - スクレイパー関数を`services/scrapers/`に移行、テスト容易化

14. **レート制限のipoousな制御**
    - 現在ログイン/登録のみ。APIエンドポイント毎の制御を拡張

---

## 10. 重要ファイルパス一覧（絶対パス）

```
/home/runner/work/ESP/ESP/app.py
/home/runner/work/ESP/ESP/models.py
/home/runner/work/ESP/ESP/database.py
/home/runner/work/ESP/ESP/cli.py
/home/runner/work/ESP/ESP/worker.py
/home/runner/work/ESP/ESP/wsgi.py
/home/runner/work/ESP/ESP/routes/*.py
/home/runner/work/ESP/ESP/services/*.py
/home/runner/work/ESP/ESP/services/translator/*.py
/home/runner/work/ESP/ESP/services/bg_remover/*.py
/home/runner/work/ESP/ESP/services/patrol/*.py
/home/runner/work/ESP/ESP/*_db.py
/home/runner/work/ESP/ESP/templates/*.html
/home/runner/work/ESP/ESP/static/css/*.css
/home/runner/work/ESP/ESP/static/js/*.js
/home/runner/work/ESP/ESP/tests/*.py
/home/runner/work/ESP/ESP/tests/fixtures/*
/home/runner/work/ESP/ESP/config/*.json
/home/runner/work/ESP/ESP/docs/*.md
/home/runner/work/ESP/ESP/alembic/versions/*.py
/home/runner/work/ESP/ESP/jobs/*.py
```

---

## 11. アーキテクチャ概要図（テキスト）

```
[ユーザー]
    ↓ HTTP
[Flask Web (esp-web)]
    ├─ ルーティング（13ブループリント）
    ├─ ビジネスロジック（services/）
    ├─ データベース（SQLAlchemy → PostgreSQL）
    ├─ 静的ファイル（/media/, /static/）
    └─ RQキュー（ジョブエンキュー）
         ↓ Redis経由
[ワーカー (esp-worker)]
    ├─ RQワーカー（スクレイピング、翻訳、背景除去）
    ├─ 共有ブラウザプール（Playwright）
    ├─ パトロールスケジューラ（APScheduler）
    └─ セレクタ修復処理
         ↓ HMAC
[Webアップロードエンドポイント]（画像処理結果）
```

### データフロー（スクレイピング例）
```
1. ユーザー → /scrape/run フォーム送信
2. Flask → ScrapeJob作成 → RQキューにEnqueue
3. ワーカー → キューから取得 → 適切スクレイパー選択
4. スクレイパー → 対象サイトからデータ抽出（ブラウザ or HTTP）
5. サービス層 → データ整形 → Productモデル作成
6. DB保存 → ユーザーに結果通知（statusページ）
```

---

## 12. 技術スタックサマリ

| カテゴリ | 技術 | バージョン |
|---------|------|----------|
| Webフレームワーク | Flask | 3.1.3 |
| ORM | SQLAlchemy | 2.0.48 |
| マイグレーション | Alembic | 1.18.4 |
| ワーカーキュー | RQ (Redis Queue) | 2.8.0 |
| キャッシュ/キュー | Redis / Valkey | 7.4.0 |
| スクレイピング | Scrapling + Playwright/Patchright | 0.4.1 / 1.58.0 |
| HTMLパース | BeautifulSoup4 | 4.14.3 |
| HTTPクライアント | requests | 2.33.0 |
| ブラウザ自動化 | Playwright | 1.58.0 |
| データ処理 | pandas | 3.0.1 |
| JSON高速化 | msgspec | 0.20.0 |
| 画像処理 | Pillow | 12.2.0 |
| 翻訳API | openai | 2.32.0 |
| 背景除去 | rembg | 2.0.75 |
| WSGIサーバー | Gunicorn | 25.1.0 |
| スケジューラ | Flask-APScheduler | 1.13.1 |
| 認証 | Flask-Login | 0.6.3 |
| フォーム/CSRF | Flask-WTF | 1.3.0 |
| テスト | pytest | 9.0.3 |
| コンテナ | Docker | - |
| クラウド | Render | - |
| データベース | PostgreSQL / SQLite | - |

---

## 13. 主要な設定変数一覧

| 変数名 | デフォルト | 必須 | 説明 |
|--------|-----------|------|------|
| `SECRET_KEY` | （開発用一時鍵） | 本番○ | Flaskセッション署名（32文字以上） |
| `APP_ENV` | development | - | `production` 本番, `development` ローカル |
| `RUNTIME_ROLE` | web | - | `web` or `worker`（起動時分岐） |
| `DATABASE_URL` | sqlite:///app.db | - | 本番はRender自動提供 |
| `REDIS_URL` | redis://localhost:6379 | - | 本番はRender自動提供 |
| `SCRAPE_QUEUE_BACKEND` | inmemory | - | `rq` or `inmemory` |
| `WEB_SCHEDULER_MODE` | enabled | - | ワーカー分離時 `disabled` |
| `WORKER_ENABLE_SCHEDULER` | 0 | - | ワーカーでパトロール有効化 `1` |
| `OPENAI_API_KEY` | - | 翻訳使用時 | OpenAI APIキー |
| `OPENAI_TRANSLATOR_MODEL` | gpt-4.1-nano | - | 翻訳モデル名 |
| `BG_REMOVAL_BACKEND` | rembg | - | `rembg` or `removal` |
| `BG_REMOVAL_MODEL` | u2netp | - | rembgモデル |
| `BG_REMOVAL_INTERNAL_SECRET` | - | 本番○ | ワーカー→Web画像アップロードHMAC |
| `BROWSER_POOL_MAX_CONTEXTS` | 8 | - | 共有ブラウザプール最大コンテキスト数 |
| `WORKER_BACKLOG_WARN_COUNT` | 25 | - | バックログ警告ジョブ数 |
| `SELECTOR_ALERT_WEBHOOK_URL` | - | - | Discord等アラート用Webhook |
| `ALLOW_PUBLIC_SIGNUP` | True | - | 一般ユーザー登録許可（本番ではFalse推奨） |
| `FORCE_HTTPS` | False | 本番○ | HTTPS強制リダイレクト |
| `HSTS_ENABLED` | False | 本番○ | Strict-Transport-Security有効化 |
| `SESSION_COOKIE_SECURE` | False | 本番○ | セッションクッキーSecure属性 |

---

## 14. コードベースの強みと現状の課題

### 強み
✅ **成熟したプロダクション対応アーキテクチャ** - 分割デプロイ、キュー、監視、自己修復など本番機能が充実
✅ **7サイト統一的スクレイピング** - 複数手法（ブラウザ/HTTP）を使い分け、セレクタ自己修復で保守性確保
✅ **翻訳・背景除去のAI統合** - OpenAI、rembg等の最新技術を実務活用
✅ **包括的なテストスイート** - E2Eからユニットまで77ファイル、CLI検証も整備
✅ **オペレーション支援** - CLIコマンド、Runbook、診断ツールが豊富

### 現状の課題（改善余地）
⚠️ **単一ワーカー制約** - インメモリキューとブラウザプールの共有によりスケールアウト困難（RQモードで改善可能だが未本番検証）
⚠️ **大規模ファイル** - `surugaya_db.py` 42,742行、`snkrdunk_db.py` 18,637行、`cli.py` 3,624行 → モジュール分割推奨
⚠️ **セレクタ修復の手動依存** - 自動適用は安全上保留、オペレータ作業負荷あり
⚠️ **パトロール間隔固定** - サイトごとの更新頻度差異を考慮した柔軟スケジューリング未対応
⚠️ **翻訳ワークフロー未自动化** - 提案→手動適用、自動適用モードなし

---

**以上、包括的なリポジトリ解析を完了しました。**
