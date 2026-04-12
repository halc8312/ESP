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
Shopify 用 CSV を生成して一括出品
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

# 4. DB マイグレーション適用
py -3 -m alembic upgrade head

# 5. 開発サーバー起動
flask run --port 5000

# 本番相当の起動（シングルワーカー必須）
gunicorn --worker-class gthread --workers 1 --threads 8 \
         --max-requests 0 --timeout 600 \
         --bind 0.0.0.0:5000 wsgi:app

# 単一 Web Service の既存本番互換:
# `SCRAPE_QUEUE_BACKEND=inmemory` の間は、web が scheduler を自動で所有する。
# このモードでは scheduler lock も file lock 側へ倒すので、Redis は不要。
# 将来 `SCRAPE_QUEUE_BACKEND=rq` に切り替えたら、web 側 scheduler は自動で無効になり、
# worker を別サービスで立てる前提になる。

# Arc 2/B4 のローカル検証例（Render 契約はまだ不要）
# 先にローカル Redis/PostgreSQL を立てたうえで:
# 推奨: repo 直下の compose を使う
docker compose -f docker-compose.local.yml up -d
# 既存の SQLite のまま queue だけ試すなら `DATABASE_URL` は省略可。
# PostgreSQL 前提で進める時は:
$env:DATABASE_URL="postgresql+psycopg://esp:esp@localhost:5432/esp_local"
$env:SCRAPE_QUEUE_BACKEND="rq"
$env:REDIS_URL="redis://localhost:6379/0"
# まず DB smoke を通す:
flask db-smoke --require-backend postgresql --apply-migrations
# detail parser だけを local dump で確認:
flask detail-fixture-smoke --site mercari --fixture-path mercari_page_dump_live.html --target-url https://jp.mercari.com/item/m71383569733
# search result dump が skeleton/challenge ではなく実結果を含むか確認:
flask search-fixture-smoke --site mercari --fixture-path search_dump.html --target-url https://jp.mercari.com/search?keyword=sneaker
# local-first の順序つき総合確認:
flask local-verify --profile full --require-backend postgresql --apply-migrations
# queue + worker + status/result までまとめて通す:
flask stack-smoke --require-backend postgresql --apply-migrations
# Product / Variant / ProductSnapshot まで保存されるかを見る:
flask stack-smoke --require-backend postgresql --apply-migrations --mode persist
# real Mercari dump を parser に通したうえで full-stack smoke:
flask stack-smoke --require-backend postgresql --apply-migrations --mode persist --fixture-site mercari --fixture-path mercari_page_dump_live.html --fixture-target-url https://jp.mercari.com/item/m71383569733
# real SNKRDUNK dump を parser に通したうえで full-stack smoke:
flask stack-smoke --require-backend postgresql --apply-migrations --mode persist --fixture-site snkrdunk --fixture-path dump.html --fixture-target-url https://snkrdunk.com/products/nike-air-max-95-og-big-bubble-neon-yellow-2025-2026
# その後 web/worker を起動:
flask run --port 5000
# Worker は別端末で dedicated entrypoint を起動:
# 定期 patrol / trash purge を持たせる worker は 1 台だけ `WORKER_ENABLE_SCHEDULER=1`
py -3 worker.py
# 旧 `run_rq_worker.py` も互換ラッパとして残してある。
# `worker.py` は既定で shared browser runtime を有効化し、Mercari browser を warm する。
# worker/RQ の現在状態を JSON で確認:
flask worker-health
# backlog warning も失敗扱いにしたい時:
flask worker-health --fail-on-warning
# 現在の単一 Web 本番へ安全に再デプロイできるかを見る:
flask predeploy-check --target single-web
# 単一 Web 本番向けの local gate を一本で回す:
flask single-web-redeploy-readiness
# 現在の単一 Web 本番向けの operator 手順をまとめて出す:
flask single-web-redeploy-checklist --base-url https://<current-web-url> --username <smoke-user> --password <smoke-password>
# 単一 Web 本番の post-deploy smoke を current 期待値で流す:
flask single-web-postdeploy-smoke --base-url https://<current-web-url>
# paid split worker の post-deploy 確認ポイントを出す:
flask render-worker-postdeploy-checklist --blueprint-path render.yaml
# 現本番と同じ single-web + inmemory 経路を実際に流す:
flask single-web-smoke --mode preview
# 将来の paid split (`web + worker + postgres + key value`) 向け readiness:
flask predeploy-check --target split-render --strict
# paid split の local rehearsal 前提を出す:
flask render-local-split-checklist --blueprint-path render.yaml
# paid split の local rehearsal gate を repo 既定の local env で一発実行:
flask render-local-split-readiness
# paid split 前の operator bundle をまとめて出す:
flask render-cutover-brief --base-url https://<esp-web-url> --username <smoke-user> --password <smoke-password>
# paid split の予算ガードが render.yaml とズレていないかを見る:
flask render-budget-guardrail-audit --blueprint-path render.yaml
# DB 単体の smoke（local PostgreSQL を指して migrate + connect + write/read を確認したい時）:
flask db-smoke --require-backend postgresql --apply-migrations
# parser 単体の fixture smoke（queue/DB を使わず detail dump を検証したい時）:
flask detail-fixture-smoke --site mercari --fixture-path mercari_page_dump_live.html --target-url https://jp.mercari.com/item/m71383569733
# local verification suite（single-web predeploy + parser fixture + db + fixture-backed stack smoke を順に回す）:
flask local-verify --profile full --require-backend postgresql --apply-migrations
# RQ + worker + API/result page まで含む full-stack smoke:
flask stack-smoke --require-backend postgresql --apply-migrations
# persist 経路まで見る full-stack smoke:
flask stack-smoke --require-backend postgresql --apply-migrations --mode persist
# real parser を通した fixture-backed full-stack smoke:
flask stack-smoke --require-backend postgresql --apply-migrations --mode persist --fixture-site mercari --fixture-path mercari_page_dump_live.html --fixture-target-url https://jp.mercari.com/item/m71383569733
flask stack-smoke --require-backend postgresql --apply-migrations --mode persist --fixture-site snkrdunk --fixture-path dump.html --fixture-target-url https://snkrdunk.com/products/nike-air-max-95-og-big-bubble-neon-yellow-2025-2026

# live site に触れない local RQ end-to-end smoke:
py -3 -m pytest tests/test_rq_scrape_e2e.py -q
```

### Render Blueprint（準備済み・未適用）

リポジトリ直下の `render.yaml` は、将来の初回有料構成向け Blueprint です。これは Render に import / sync するまで何も起こりません。さらに、service 名は現在の単一 Web 本番と意図的に分けてあり、`autoDeployTrigger: off` なので、ファイルを commit しただけで現本番へ影響しないようにしています。

- 現本番を維持する間は、Render 側の単一 Web Service を従来どおり `SCRAPE_QUEUE_BACKEND=inmemory` のまま使う
- `render.yaml` を sync するのは、`rq + worker + postgres + key value` の paid split を実際に確認したい段階になってから
- Blueprint の web は `/healthz` を health check に使い、worker は `python worker.py` で起動する
- 画像とショップロゴの永続化がまだ filesystem 前提なので、Blueprint では web に小さい persistent disk を付け、`IMAGE_STORAGE_PATH=/var/data/images` を使う

---

## 8. 環境変数

| 変数名 | デフォルト | 説明 |
|--------|----------|------|
| `SECRET_KEY` | なし（必須） | Flask セッション署名キー |
| `DATABASE_URL` | `sqlite:///mercari.db` | DB 接続文字列 |
| `SCHEMA_BOOTSTRAP_MODE` | `auto` (`web`/`cli`) | `alembic` 優先で schema を適用。Alembic 未導入時は `legacy` にフォールバック |
| `SCRAPE_QUEUE_BACKEND` | `inmemory` | `inmemory` または `rq`。`rq` はローカル Redis で先行検証可能 |
| `REDIS_URL` | `redis://localhost:6379/0` | `SCRAPE_QUEUE_BACKEND=rq` 時の接続先 |
| `SCRAPE_QUEUE_NAME` | `scrape` | RQ queue 名 |
| `RQ_BURST` | `false` | `worker.py` を burst モードで1回だけ動かすか |
| `RQ_WITH_SCHEDULER` | `false` | RQ の scheduler 機能を worker に有効化するか。通常は `false` |
| `SCRAPE_JOB_HEARTBEAT_SECONDS` | `30` | running job の heartbeat 間隔 |
| `SCRAPE_JOB_STALL_TIMEOUT_SECONDS` | `900` | heartbeat が止まった running job を failed 扱いに切り替える秒数 |
| `SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS` | `60` | durable state は non-terminal だが Redis/RQ 上に job 本体が見つからない場合に failed 扱いへ切り替える猶予秒数 |
| `WORKER_ENABLE_SCHEDULER` | `false` | patrol / trash purge の APScheduler をこの worker が所有するか。`true` にする worker は 1 台だけ |
| `WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP` | `true` | worker 起動時に、stall timeout を超えた `running` job を durable state 上で `failed` に掃除するか |
| `WORKER_BACKLOG_WARN_COUNT` | `25` | worker 起動時 backlog 診断で warning を出す queued job 件数しきい値。`0` で無効 |
| `WORKER_BACKLOG_WARN_AGE_SECONDS` | `900` | worker 起動時 backlog 診断で warning を出す oldest queued/running age しきい値。`0` で無効 |
| `SELECTOR_ALERT_WEBHOOK_URL` | unset | selector healer / repair candidate 通知の送信先 webhook。Discord raw webhook も利用可 |
| `OPERATIONAL_ALERT_WEBHOOK_URL` | unset | worker backlog などの silent operational alert 送信先 webhook |
| `OPERATIONAL_ALERT_COOLDOWN_SECONDS` | `900` | 同一 operational alert の再送 cooldown |
| `OPERATIONAL_ALERT_MAX_PER_WINDOW` | `10` | operational alert の window 内最大送信数 |
| `OPERATIONAL_ALERT_WINDOW_SECONDS` | `300` | operational alert の rate-limit window 秒数 |
| `WEB_SCHEDULER_MODE` | `auto` | `auto` は `SCRAPE_QUEUE_BACKEND=inmemory` の web だけ scheduler を持つ。`enabled` / `disabled` で明示上書き可能 |
| `SCHEDULER_LOCK_BACKEND` | `auto` | scheduler lock。`auto` は single-service web/inmemory では file lock、worker/rq 側では Redis lock を優先する |
| `SCHEDULER_LOCK_KEY` | `esp:scheduler:lock` | Redis lock key |
| `SCHEDULER_LOCK_TTL_SECONDS` | `120` | Redis scheduler lock の TTL |
| `ENABLE_SHARED_BROWSER_RUNTIME` | `false` (`worker.py` では `true` 既定) | shared Playwright browser runtime を使うか |
| `WARM_BROWSER_POOL` | `false` (`worker.py` では `true` 既定) | worker 起動時に browser pool を warm するか |
| `BROWSER_POOL_WARM_SITES` | `mercari` | 起動時に warm する browser site 一覧 |
| `BROWSER_POOL_MAX_CONTEXTS` | `1` | shared browser 1 プロセスあたりの同時 page/context 実行上限 |
| `BROWSER_POOL_RESTART_ATTEMPTS` | `1` | browser crash 時の自動再起動回数 |
| `BROWSER_POOL_MAX_TASKS_BEFORE_RESTART` | `0` | 0 より大きい時、同一 browser を使うジョブ回数の上限。超えたら次ジョブ開始前に計画的 recycle |
| `BROWSER_POOL_MAX_RUNTIME_SECONDS` | `0` | 0 より大きい時、browser 生存時間の上限。超えたら次ジョブ開始前に計画的 recycle |
| `BROWSER_POOL_STARTUP_TIMEOUT_SECONDS` | `60` | shared browser 起動タイムアウト |
| `MERCARI_USE_BROWSER_POOL_DETAIL` | `false` (`worker.py` では `true` 既定) | Mercari detail DOM fetch を browser pool 経由にする |
| `MERCARI_PATROL_USE_BROWSER_POOL` | `false` (`worker.py` では `true` 既定) | Mercari patrol DOM fetch を browser pool 経由にする |
| `SNKRDUNK_USE_BROWSER_POOL_DYNAMIC` | `false` (`worker.py` では `true` 既定) | SNKRDUNK search と dynamic detail fallback を browser pool 経由にする |
| `LOG_LEVEL` | `INFO` (`worker.py`) | worker/browser pool instrumentation の出力レベル |
| `PORT` | `10000` | Gunicorn バインドポート |
| `IMAGE_STORAGE_PATH` | `static/images` | ダウンロード画像の保存先。Render disk を付ける場合は `/var/data/images` を推奨 |
| `MERCARI_USE_NETWORK_PAYLOAD` | `false` | メルカリ API インターセプト有効化 |
| `{SITE}_DETAIL_CONCURRENCY` | サイト依存 | 詳細ページの並列取得数 |
| `{SITE}_DETAIL_TIMEOUT` | サイト依存 | タイムアウト秒数 |
| `{SITE}_DETAIL_RETRIES` | サイト依存 | リトライ回数 |
| `{SITE}_DETAIL_BACKOFF` | サイト依存 | リトライ間隔（秒） |

`{SITE}` には `MERCARI`, `RAKUMA`, `YAHOO`, `YAHUOKU`, `SURUGAYA`, `OFFMALL`, `SNKRDUNK` が入ります。

shared browser runtime を有効にした worker は、起動時の durable backlog 要約、browser warm・restart・close 前 health snapshot を worker log に出します。backlog warning がしきい値を超えたままなら、`OPERATIONAL_ALERT_WEBHOOK_URL` が設定されている場合だけ silent alert も送れます。`{SITE}_BROWSER_POOL_MAX_CONTEXTS`、`{SITE}_BROWSER_POOL_MAX_TASKS_BEFORE_RESTART`、`{SITE}_BROWSER_POOL_MAX_RUNTIME_SECONDS` を使うと site 別に上限を上書きできます。

`flask predeploy-check` は deploy 前の安全確認用です。`--target single-web` は現在の単一 Render Web Service 互換を、`--target split-render` は将来の `$61/month` 想定構成を前提に、queue / schema bootstrap / scheduler / storage の blocker と warning を JSON で返します。CLI 実行時には current DB に対する `schema-drift-check` も併せて走るので、軽い再デプロイ前確認でも additive drift を見落としにくくしています。

`flask single-web-redeploy-readiness` は、現在の Render 単一 Web 本番を再デプロイしてよいかをローカルで判定する gate です。`predeploy-check --target single-web` と `local-verify --profile parser` を一つに束ねるので、routine redeploy 前の確認を一発で回せます。手順全体は `docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md` にまとめています。

`flask single-web-redeploy-checklist` は、現在の Render 単一 Web 本番を安全に再デプロイするための operator 向け JSON checklist です。local gate、Dashboard 上で崩してはいけない env 前提、post-deploy smoke、rollback を一つにまとめます。日々の DOM/UI 修正に伴う再デプロイでは、まずこれを出して順番どおりに確認する運用が安全です。post-deploy smoke のコマンド列には cautious default として `--retries 4 --retry-delay-seconds 2` を含めています。手順全体は `docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md` にまとめています。

`flask single-web-postdeploy-smoke --base-url https://...` は、現在の Render 単一 Web 本番向け post-deploy smoke です。`render-postdeploy-smoke` の current single-web 版で、`queue_backend=inmemory`、`runtime_role=web`、`scheduler_enabled=true` を前提に `/healthz`、`/login`、`/scrape`、`/api/scrape/jobs` を確認します。`--username` と `--password` を付けると authenticated route も見られ、`--ensure-user` を付けると必要時だけ `/register` を試します。deploy 直後の cold start や一時的な 502/503 を吸収したい時は `--retries` と `--retry-delay-seconds` で再試行回数を上げられます。

`flask single-web-smoke` は、現本番と同じ `single-web + SCRAPE_QUEUE_BACKEND=inmemory` の互換 path を live site なしで end-to-end に確認するコマンドです。内部 smoke payload を使って job enqueue、`/api/scrape/status/<job_id>`、`/api/scrape/jobs`、`/scrape/result/<job_id>` まで確認します。`--mode preview` では DB に商品が保存されないこと、`--mode persist` では保存経路まで確認できます。

`flask db-smoke` は `DATABASE_URL` に対する明示的な DB smoke です。`--apply-migrations` を付けると Alembic/legacy 設定に従って schema を適用したうえで、接続・簡易 write/read・主要テーブル存在確認を行います。local PostgreSQL を立てた段階で、まずこれを通してから web/worker の end-to-end に進めるのが安全です。

`flask schema-drift-check` は、persistent DB に additive patchset の不足が残っていないかを見る軽い監査です。特に既存 SQLite を持ったまま再デプロイする前に有効で、今回のような `scrape_jobs.context_payload` 欠落も deploy 前に見つけられます。

`flask detail-fixture-smoke` は queue / Redis / DB を使わずに local detail dump を real parser へ通すための軽量チェックです。`--strict` を付けると title / price / image / page_type などの warning を blocker 扱いにできます。日々の DOM 修正時はこれで parser 単体を先に見てから `stack-smoke` へ進めるのが安全です。

`flask search-fixture-smoke` は local search-result dump が「実際の item URL を含む検索結果」なのか、「skeleton / challenge / 未描画ページ」なのかを素早く判定する軽量チェックです。現在は Mercari search dump に対応していて、`item_urls_missing` や `search_results_not_rendered` を blocker として返します。日々の DOM 修正時に、detail 側へ進む前の入口チェックとして使えます。

`flask local-verify` は、いま積み上げた local-first 検証を順序つきでまとめて回すコマンドです。すべての profile で current DB に対する `schema-drift-check` を先に走らせるので、既存 SQLite や local PostgreSQL に additive drift が残っている状態を日常の再デプロイ前に拾えます。`--profile parser` は single-web predeploy と schema drift 監査に続いて `single-web-smoke --mode preview` を実行し、その後に detail fixture 群と、`search_dump.html` があれば Mercari search fixture 判定も advisory step として含みます。`--profile stack` は split-render を含む advisory predeploy + db-smoke + fixture-backed stack smoke、`--profile full` はその両方に加えて `single-web-smoke --mode persist --fixture-site mercari ...` と `single-web-smoke --mode persist --fixture-site snkrdunk ...` も含みます。predeploy/search 系の advisory step は「今ある dump の質」や「切替準備の不足」を見える化するために出し、suite 全体の成否は schema drift / single-web / parser / db / stack の実動作で判定します。daily の DOM 修正後は `parser`、しっかり確認する時は `full` を流す運用を想定しています。

`flask render-cutover-readiness` は、最初の paid Render split に入ってよいかをローカルで判定する C4 用 gate です。current single-web predeploy は advisory として残しつつ、persistent DB の `schema-drift-check`、split-render predeploy、split worker health、`local-verify --profile full` を一つに束ねます。paid activation 前は `flask render-local-split-checklist` で local PostgreSQL/Redis/RQ の前提と順序を確認したうえで、`flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict` を通してから進めてください。手順全体は `docs/RENDER_CUTOVER_RUNBOOK.md` にまとめています。

`flask render-blueprint-audit` は `render.yaml` の静的監査です。`esp-web` / `esp-worker` / `esp-keyvalue` / `esp-postgres` の service 名、`autoDeployTrigger: off`、`/healthz`、`python worker.py`、managed `DATABASE_URL` / `REDIS_URL`、manual secret env の棚卸しを確認します。Render Dashboard に入る前の secret/env チェックとして使えます。

`flask render-budget-guardrail-audit --blueprint-path render.yaml` は、repo に記録した budget guardrail 前提と `render.yaml` の plan を照合する監査です。いまの前提では `esp-web=starter`, `esp-worker=standard`, `esp-keyvalue=starter`, `esp-postgres=basic-1gb` を要求し、core recurring cost estimate は `$61/month` として扱います。これは repo に固定した planning assumption で、actual purchase 前には Render 側の価格再確認が別途必要です。

`flask render-local-split-checklist` は、paid split をローカルで rehearse するための operator 向け JSON checklist です。`docker-compose.local.yml`、local PostgreSQL/Redis 用 env 契約、PowerShell の env export 例、local PostgreSQL/Redis の TCP 到達確認、`db-smoke` / `worker-health` / `local-verify --profile full` / `render-cutover-readiness` の実行順を一つにまとめます。`render-cutover-readiness` が落ちた時に「何を揃えれば gate が通るか」を先に見たい時は、まずこれを出してください。

`flask render-local-split-readiness` は、repo に固定した local split env を一時適用して `render-local-split-checklist` と `render-cutover-readiness --strict` をまとめて回す one-shot gate です。shell に手で env を積まずに paid split rehearsal を再現したい時は、まずこれを使うのが安全です。

`flask render-cutover-brief` は、初回 paid cutover に必要な operator 情報をまとめて出す bundle です。`render-budget-guardrail-audit`, `render-dashboard-inputs`, `render-worker-postdeploy-checklist`, `render-local-split-readiness`, `render-cutover-checklist` を 1 回で集約するので、契約直前に確認コマンドを行き来しなくて済みます。

`flask render-dashboard-inputs` は `render.yaml` から Dashboard 入力用の env 一覧を JSON で出します。service ごとの `manual_envs`、`managed_envs`、`fixed_envs` を分けて見られるので、「Render 側で手入力するもの」と「Blueprint に任せるもの」を混ぜにくくなります。

`flask render-postdeploy-smoke --base-url https://...` は、初回 paid activation 後の web 健全性チェックです。`/healthz` の JSON を見て `runtime_role=web`、`queue_backend=rq`、`scheduler_enabled=false` を確認し、加えて `/login`、`/scrape`、`/api/scrape/jobs` が 500 を返していないことを見ます。`--username` と `--password` を付けるとログイン後の `/scrape` と `/api/scrape/jobs` も確認するので、今回 staging で実際に壊れた「認証後にだけ 500 になる」系も Deploy 後すぐに検知できます。初回 smoke user がまだ存在しない場合は `--ensure-user` を付けると、login が通らなかった時だけ `/register` を試してから authenticated route smoke へ進みます。deploy 直後の一時 502/503 や cold start を見越すなら `--retries` と `--retry-delay-seconds` を増やして判定を安定化できます。

`flask render-worker-postdeploy-checklist --blueprint-path render.yaml` は、paid split の worker post-deploy で見るべき log marker と runtime 契約を JSON で出します。`esp-worker` の fixed / managed / manual env、`python worker.py` 前提、scheduler owner、browser warm、backlog warning 閾値を `render.yaml` から読み取り、worker 起動ログで何を確認すべきかを operator 向けに固定します。

`flask render-cutover-checklist` は、初回 paid cutover 時の実行順を JSON で出します。pre-cutover command、Dashboard 上の手動 step、manual secret env、post-deploy command、rollback step を一つにまとめるので、operator が runbook と CLI を行き来しなくて済みます。pre-cutover command には `schema-drift-check` と `render-local-split-checklist` も含まれるので、persistent DB の additive drift と local split rehearse 手順を見落としにくくなります。`--base-url` と smoke user を渡しておけば、post-deploy smoke のコマンド列まで具体化され、deploy 直後の false negative を減らすために `--retries 4 --retry-delay-seconds 2` も自動で含まれます。

`flask stack-smoke` は live site に触れない full-stack smoke です。local DB/Redis に対して一時ユーザーを作り、internal smoke payload を preview または persist mode で RQ に enqueue し、`worker.py` 相当の burst worker で処理し、最後に `/api/scrape/status/<job_id>`、`/api/scrape/jobs`、`/scrape/result/<job_id>` を確認します。`--mode persist` を付けると `Product` / `Variant` / `ProductSnapshot` まで検証します。通常は完了後に一時 user/job/product を cleanup し、`--keep-artifacts` を付けた時だけ残します。`--fixture-site mercari` や `--fixture-site snkrdunk` を付けると internal dummy item の代わりに local HTML dump を real parser に通した結果で同じ smoke を流せます。

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
