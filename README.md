# ESP — 日本向けマーケットプレイス 商品管理・スクレイピングシステム

> 日本語ECサイトの商品情報を収集し、商品・販売価格・公開カタログの管理と、Shopify / eBay向けCSV出力を支援するFlaskアプリケーションです。

このREADMEは、2026-09-12時点の実装（照合元: `72a8e6df47ebd9ec2bf4d4047b4511f325ec4317`）に合わせて整理しています。**実装が存在することと、外部サイト・外部API・デプロイ先で現在利用できることは別です。** 調査時に見つかった制約・改善事項は[リポジトリ横断レビュー](docs/REPOSITORY_REVIEW_2026-09-12.md)を参照してください。

## AI Agent Quick Start

変更前に[AGENTS.md](AGENTS.md)を読み、実装・設定・運用手順を照合してください。

- 本番の運用前提は **`esp-web` + `esp-worker` + `esp-keyvalue` + `esp-postgres`** のsplit構成です。[render.yaml](render.yaml)がリポジトリ側の構成契約です。Dashboardの実設定・稼働状況は別途確認し、READMEだけから稼働確認済みと判断しないでください。
- `worker.py`はRQ専用entrypointです。定期処理のownerはworker側に置き、web側のschedulerは無効にします。`single-web`系はローカル・legacy互換用途です。
- 公開カタログに`source_url`、`site`、仕入れ先URLなどの内部情報を出さないでください。ユーザー・ショップ・価格表の所有権境界を維持してください。
- 本番の`SECRET_KEY`、DB、Redis、キュー名、画像処理用共有secretの契約をweb / worker間で揃えてください。設定不足を開発用デフォルトで回避しないでください。
- `llama.cpp/`は明示指示がない限り変更しないでください。デプロイ・DB移行・サービス増設は、文書修正とは別の操作です。

| 変更箇所 | 主な入口 |
|---|---|
| 商品一覧・編集 | `routes/main.py`, `routes/api.py`, `routes/products.py`, `templates/index.html`, `templates/product_detail.html` |
| 商品抽出 | `routes/scrape.py`, `jobs/scrape_tasks.py`, `services/scrape_request.py` |
| 公開価格表・問い合わせ | `routes/pricelist.py`, `routes/catalog.py`, `routes/catalog_requests.py` |
| 翻訳・画像処理 | `routes/translation.py`, `services/translator/`, `routes/bg_removal.py`, `services/bg_remover/` |
| 起動・運用 | `app.py`, `worker.py`, `services/worker_runtime.py`, `cli.py`, `render.yaml` |

主な検証入口は`python -m pytest tests/test_e2e_routes.py -q`と`python -m pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q`です。本番splitの運用手順は[RENDER_CUTOVER_RUNBOOK](docs/RENDER_CUTOVER_RUNBOOK.md)、legacy互換は[SINGLE_WEB_REDEPLOY_RUNBOOK](docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md)を参照してください。

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

ESPは、仕入れ候補の収集から編集、翻訳、価格設定、公開カタログ、CSV出力までを管理します。

```text
キーワード / 商品URL / 検索結果URL
    → 抽出ジョブ → プレビュー・選択登録 → Product / Variant等を保存
    → 商品編集・翻訳・画像加工・販売価格設定
    → 公開カタログ / Shopify・eBay向けCSV

定期パトロール → 対象商品の価格・状態・在庫をバッチ更新
```

CSV出力は外部サービスへのAPI自動出品・決済完了を意味しません。公開カタログのリクエスト受付も、在庫予約や決済処理とは別の機能です。

## 2. 主要機能

| 分野 | 実装されている機能と条件 |
|---|---|
| 商品抽出 | キーワード、単品URL、検索結果URLの判定・抽出、価格帯・除外キーワードのフィルター、結果プレビューと選択登録。Record Cityを含む8サイトの抽出経路があります |
| 非同期処理 | 開発・互換用`inmemory`と、本番split用`rq`。抽出ジョブの状態・進捗・結果・イベントはDBにも記録します |
| 商品管理 | 日本語・英語のタイトル／説明、タグ、Vendor、SEO、バリエーション、画像URL・アップロード・並び替え、説明文テンプレート、ゴミ箱・アーカイブ、履歴 |
| 登録・価格 | 手動登録、CSVインポート、商品リストのみの登録（`is_listed=False`）、登録時のデフォルト価格ルール適用、商品別・バリエーション別販売価格 |
| 翻訳 | Argos / OpenAI、登録時自動翻訳、提案のレビュー・適用、原文ハッシュと手動編集の保護。OpenAI設定でもArgosへのフォールバック経路があります |
| 画像白抜き | rembgによる背景除去ジョブ、結果の確認・適用・却下。モデル、キュー、webへの結果返送設定が必要です。実装済みですが利用可能性は実推論で確認してください |
| 公開カタログ | トークンURL、複数レイアウト、テーマ、検索、タグ絞り込み、Quick View、ショップロゴ、公開期限、価格・為替関連表示、閲覧記録 |
| カタログリクエスト | 商品・価格のスナップショットを伴う受付と状態管理。予約・注文確定・決済ではありません |
| アカウント | ログイン、ユーザー・ショップごとの管理、`student` / `admin`ロール、利用停止状態。一般ユーザー作成と管理権限の付与は別操作です |
| メール | Resend送信基盤と設定確認・テスト送信CLI。標準は無効で、APIキーだけでは送信を開始しません。業務イベントの自動通知を一括して保証する機能ではありません |
| 運用 | ヘルスチェック、worker / scheduler heartbeat、抽出観測・アラート、セレクター修復候補と検証、DB・stack・デプロイ診断CLI |

セレクター修復は設定・対象サイト・検証条件に依存します。「全サイトのDOM変更を無条件で自動修復する」ものとして扱わないでください。

## 3. 対応スクレイピングサイト

表の「実装あり」はコード上の対応範囲です。サイト側の変更・アクセス制限や実行環境により取得に失敗する場合があり、現在の到達性を保証しません。

| サイト | 主なドメイン | 検索・詳細抽出 | 定期パトロール |
|---|---|---|---|
| メルカリ | `jp.mercari.com` | 実装あり | 対象 |
| ラクマ | `fril.jp`, `item.fril.jp` | 実装あり | 対象 |
| Yahoo!ショッピング | `shopping.yahoo.co.jp` | 実装あり | 対象 |
| ヤフオク! | `auctions.yahoo.co.jp` | 実装あり | 対象 |
| 駿河屋 | `suruga-ya.jp` | 実装あり | 対象 |
| オフモール | `netmall.hardoff.co.jp` | 実装あり | 対象 |
| SNKRDUNK | `snkrdunk.com` | 実装あり | 対象 |
| Record City（レコードシティ） | `recordcity.jp` | 実装あり | **現行の対象リストには含まれない** |

根拠: [抽出リクエスト](services/scrape_request.py)、[抽出タスク](jobs/scrape_tasks.py)、[監視サービス](services/monitor_service.py)。抽出対応数とパトロール対応数を混同しないでください。

取得にはサイト別のHTTP・DOMパーサー・Scrapling・Playwright / Patchright経路を使います。フォールバックもあるため、固定の「各サイトは常にHTTPのみ」という説明は避けています。Record Cityは`RECORDCITY_FETCH_PROVIDER`で経路を明示選択し、Blueprintのworkerは`browser` + `persistent-chrome`を使用します。外部providerのcredentialを設定しただけではproduction取得経路を変更しません。

`persistent-chrome`はRecord City専用のbranded Chrome + Patchright persistent contextをXvfb上で動かす構成です。**DockerfileにはChromeのインストール処理があります。** 通常のローカル既定値`headless`とは必要なブラウザ・表示環境が異なります。

## 4. 技術スタック

直接依存のバージョンは[requirements.txt](requirements.txt)、開発用は[requirements-dev.txt](requirements-dev.txt)を参照してください。直接依存の固定だけで推移依存まで固定されるわけではありません。

| 層 | 主な構成 |
|---|---|
| 実行環境 | Python 3.11をDocker / CIで使用 |
| Web・認証 | Flask、Flask-Login、Flask-WTF、Gunicorn |
| DB・マイグレーション | SQLAlchemy、Alembic、psycopg、SQLite / PostgreSQL |
| ジョブ・定期処理 | Redis / Valkey、RQ、Flask-APScheduler |
| 取得・解析 | Scrapling、Playwright、Patchright、curl_cffi、BeautifulSoup4、requests、msgspec |
| 翻訳・画像 | Argos Translate、OpenAI SDK、Pillow、rembg |
| 表示・安全性 | Jinja2、CSS / JavaScript、nh3によるHTMLサニタイズ |
| コンテナ・運用 | Python 3.11-slim、Tini、Xvfb、Render Blueprint、GitHub Actions |

SQLiteはローカルの既定値、PostgreSQLは本番splitの構成です。MySQL用ドライバーや検証環境はこの構成に含まれないため、MySQLを同等の検証済みバックエンドとしては案内しません。

## 5. システムアーキテクチャ

```text
利用者・公開カタログ閲覧者
             │
             ▼
esp-web: Gunicorn / Flask
  routes / 認証・所有権確認 / 公開表示 / CSV / media配信
       │                     │
       │ enqueue             │ 永続データ
       ▼                     ▼
esp-keyvalue            esp-postgres
  Redis / Valkey          商品・アカウント・価格表
  RQ / lock / heartbeat   ジョブ・提案・画像処理状態・観測
       │                     ▲
       ▼                     │
esp-worker: tini -- python worker.py
  RQ SimpleWorker / browser runtime
  抽出・翻訳・画像処理 / worker所有APScheduler
       │
       └─ 画像処理結果を共有secretで認証してwebへ返送
                         │
                         ▼
                 webの永続ディスク
                 /var/data/images
```

### 本番splitとローカル互換の違い

| 項目 | `rq` / split | `inmemory` / 単一プロセス |
|---|---|---|
| 実行主体 | dedicated `worker.py` | webプロセス内のキュー |
| 状態 | RQとDBのジョブ記録を利用 | プロセス内の実行管理。DB記録があっても実行中スレッドは再起動で消失 |
| 並列性 | 現行entrypointは`SimpleWorker`。`scrape`と`media`のキューを受け持つ | ThreadPoolExecutorのHTTP / browser実行枠 |
| scheduler | 指定したworker 1台が所有 | `WEB_SCHEDULER_MODE=auto`ではweb / inmemory側が所有 |
| 用途 | 本番の運用前提 | 開発・legacy互換確認 |

inmemory側の実行枠をRQ workerの同時実行数として読み替えないでください。`MEDIA_QUEUE_NAME`を別名にしても、自動で画像専用workerが作られるわけではありません。現行のworkerは複数キューを読む1つのworkerです。

DockerのGunicorn既定値は`--worker-class gthread --workers 1 --threads 8 --max-requests 0 --timeout 600`です。この構成は維持していますが、「本番もinmemoryだから必ず1」という説明ではありません。inmemoryのプロセス分離制約と、本番splitの増設・並列化設計は分けて検証してください。

## 6. データベース構造

定義は[models.py](models.py)、接続・schema bootstrapは[database.py](database.py)、変更履歴は`alembic/`を参照してください。

| 分野 | 主なモデル |
|---|---|
| 所有者・ショップ | `User`, `Shop` |
| 商品・販売価格 | `Product`, `Variant`, `ProductSnapshot`, `PricingRule`, `DescriptionTemplate`, `ExclusionKeyword` |
| 公開カタログ | `PriceList`, `PriceListItem`, `CatalogPageView` |
| リクエスト受付 | `CatalogRequest`, `CatalogRequestItem` |
| 抽出ジョブ | `ScrapeJob`, `ScrapeJobEvent` |
| 翻訳・画像処理 | `TranslationSuggestion`, `ImageProcessingJob` |
| 運用・修復 | `SelectorRepairCandidate`, `SelectorActiveRuleSet`, `ScrapeHealthObservation`, `ScrapeHealthState`, `ScrapeHealthDelivery` |
| 為替 | `ExchangeRate` |

`Product.site` / `source_url`は内部の取得元情報です。`archived`、`deleted_at`、`is_listed`は別の状態であり、売切れ状態と同一ではありません。`last_patrolled_at`、`next_patrol_at`、`patrol_fail_count`がパトロールの選択・再試行に使われます。

`Variant.price`は取得元価格、`Variant.selling_price`は販売価格の上書きです。販売価格の解決では明示的な`0`も有効です。バリエーション別価格、商品共通価格、旧データの取得元価格へのフォールバックや比率調整は[pricing_service.py](services/pricing_service.py)に集約されています。

翻訳提案には`worker_token` / `lease_expires_at`がありますが、画像処理ジョブに同じ回復契約があると仮定しないでください。ジョブがDBに存在することだけでは、再起動後の自動再開・重複実行防止は保証されません。

## 7. セットアップ・起動方法

以下のシェル例は**Bash**です。PowerShellでは環境変数を`$env:NAME = "value"`形式に読み替え、異なるシェルの構文を同じブロックで混用しないでください。PythonはまずDocker / CIと同じ3.11で検証してください。

### ローカルPython: SQLite + inmemory

```bash
git clone https://github.com/halc8312/ESP.git
cd ESP
python3.11 -m venv .venv
source .venv/bin/activate

# 実行のみならrequirements.txt、開発・テスト込みならこちら
python -m pip install -r requirements-dev.txt
scrapling install
patchright install chromium

export FLASK_APP=app
export APP_ENV=development
export RUNTIME_ROLE=""
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export DATABASE_URL="sqlite:///mercari.db"
export SCRAPE_QUEUE_BACKEND=inmemory
# 開発中に意図せず登録商品を外部巡回しないよう、ここでは無効
export WEB_SCHEDULER_MODE=disabled

# 接続先は開発DBであることを確認。schema適用とwrite/read確認を行う
python -m flask db-smoke --apply-migrations

# 対話式でユーザー名・パスワードを入力。作成時のroleはstudent
python -m flask create-user
# 管理画面が必要なユーザーにだけ付与。USERNAMEを作成名に置換
python -m flask set-user-role USERNAME admin

python -m flask run --host 127.0.0.1 --port 5000
```

[.env.example](.env.example)は設定のひな型です。`.env`を置くだけで、`worker.py`やGunicornを含むすべての起動方法が自動読込するとは仮定しないでください。自分で管理する`.env`を利用する場合、Bashでは`set -a; . ./.env; set +a`などで明示的に読み込みます。secretは保存先・権限を管理し、Gitに追加しないでください。

`python -m alembic upgrade head`は明示的なマイグレーション適用コマンドです。既存DBへの適用前はバックアップと接続先を確認してください。

### ローカルsplit: PostgreSQL + Redis + dedicated worker

[ローカルCompose](docker-compose.local.yml)はPostgreSQLとRedisを起動します。web / workerアプリ自体は別端末で起動します。Composeの固定パスワード・公開ポート・Redis永続化無効設定は**開発用**であり、本番用のデータ保全設定ではありません。

```bash
docker compose -f docker-compose.local.yml up -d

# webとworkerの両端末で同じ仮想環境・以下の設定・SECRET_KEYを使用する
export FLASK_APP=app
export APP_ENV=development
export RUNTIME_ROLE=""
export DATABASE_URL="postgresql+psycopg://esp:esp@localhost:5432/esp_local"
export SCRAPE_QUEUE_BACKEND=rq
export REDIS_URL="redis://localhost:6379/0"
export SCRAPE_QUEUE_NAME=scrape
export MEDIA_QUEUE_NAME=media
export WEB_SCHEDULER_MODE=disabled

python -m flask db-smoke --require-backend postgresql --apply-migrations
# liveサイトを使わない内部payloadによるstack確認
python -m flask stack-smoke --require-backend postgresql --apply-migrations

# 端末A
python -m flask run --host 127.0.0.1 --port 5000
```

```bash
# 端末B: 上の共通設定を読み込み済みで実行
# 定期処理を担当させるworkerだけ1にする。巡回対象への外部通信が発生する
export WORKER_ENABLE_SCHEDULER=1
python worker.py
```

`WORKER_ENABLE_SCHEDULER`の通常の既定値は`false`です。定期巡回が不要な検証では`0`にしてください。旧`run_rq_worker.py`は互換ラッパーです。

画像白抜きをsplitで試す場合は、web / workerに同じ`BG_REMOVAL_INTERNAL_SECRET`と`BG_REMOVAL_BACKEND=rembg`を設定します。ローカルworkerには`ESP_WEB_INTERNAL_URL=http://127.0.0.1:5000`など、**workerから到達できるweb URL**も指定してください。モデルの利用可能性は別途確認が必要です。

### Docker: ローカル単一webの確認

本番はこの単一web例ではなく、後述のBlueprintを使用します。コンテナは`myuser`で実行されるため、DB・画像・インポートプレビューには書込み可能な場所が必要です。`/app/mercari.db`への暗黙の書込みや、コンテナ内だけの保存に依存しないでください。

```bash
docker build -t esp-app .
docker volume create esp-local-data
# 新規の開発用volumeだけを初期化。アプリ本体はrootで実行しない
# 既存データのvolumeに対して無条件に所有権を変更しないこと
docker run --rm --user root -v esp-local-data:/var/data esp-app \
  sh -c 'mkdir -p /var/data/images /var/data/import_previews && chown -R myuser:myuser /var/data'

export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
docker run --rm --name esp-local -p 127.0.0.1:10000:10000 \
  -v esp-local-data:/var/data \
  -e APP_ENV=development -e RUNTIME_ROLE= -e SECRET_KEY \
  -e SCRAPE_QUEUE_BACKEND=inmemory -e WEB_SCHEDULER_MODE=disabled \
  -e DATABASE_URL=sqlite:////var/data/mercari.db \
  -e IMAGE_STORAGE_PATH=/var/data/images \
  -e IMPORT_PREVIEW_STORAGE_PATH=/var/data/import_previews \
  esp-app

# 起動後、別端末から対話式でユーザーを作成
# docker exec -it esp-local python -m flask --app app create-user
# docker exec -it esp-local python -m flask --app app set-user-role USERNAME admin
```

この例はSQLite・画像をvolumeに保存するローカル確認用で、RQや本番HTTPS・外部サービスを検証するものではありません。

### Render Blueprint / split topology

[render.yaml](render.yaml)と[RENDER_CUTOVER_RUNBOOK](docs/RENDER_CUTOVER_RUNBOOK.md)を参照してください。Blueprintにはweb / worker / PostgreSQL / Key Valueが定義され、`autoDeployTrigger: off`です。READMEの更新はデプロイ操作ではありません。

web / workerで`DATABASE_URL`、`REDIS_URL`、`SECRET_KEY`、キュー名、画像処理用secretを揃えます。webは`WEB_SCHEDULER_MODE=disabled`、定期処理ownerのworkerは`WORKER_ENABLE_SCHEDULER=1`です。`SCHEMA_BOOTSTRAP_MODE=auto`ではPostgreSQLのAlembic upgradeをadvisory lockで直列化します。移行権限・バックアップは別途必要です。

webの画像保存先は永続ディスク上の`/var/data/images`です。workerからの画像返送にはBlueprintで渡す`WEB_INTERNAL_HOST`と`WEB_INTERNAL_PORT=8080`を使います。内部ホスト名を単に`esp-web`と決め打ちしないでください。TiniはPythonを監督し、Python側がRecord City用Xvfbを起動・停止します。

## 8. 環境変数

以下は主要な設定です。**一般の既定値、worker entrypointの上書き、Blueprintの明示設定は異なります。** 詳細は[app.py](app.py)、[security_config.py](security_config.py)、[worker.py](worker.py)、[render.yaml](render.yaml)および各サービスを確認してください。

### 基本・セキュリティ

| 変数 | 既定値・扱い |
|---|---|
| `APP_ENV` / `RUNTIME_ROLE` | 開発では`development` / 空。本番では`production` / `web`または`worker`。`RUNTIME_ROLE=web/worker`だけでも本番セキュリティ判定の対象 |
| `SECRET_KEY` | 開発用既定値あり。本番は未設定・既知の開発値・32文字未満を拒否。web / workerで同じ十分長いランダム値を使用 |
| `DATABASE_URL` | `sqlite:///mercari.db`。本番はPostgreSQL。`postgres://` / `postgresql://`はpsycopg用URLへ正規化 |
| `SCHEMA_BOOTSTRAP_MODE` | Blueprintは`auto`。新しいschema変更はAlembicに追加し、legacy救済patchを通常の変更手段にしない |
| `REDIS_URL` / `VALKEY_URL` | 本番レート制限は共有ストア設定必須。splitのDB・キュー契約では両プロセスに同じ`REDIS_URL`を明示 |
| `ALLOW_PUBLIC_SIGNUP` | 開発は通常有効、本番は既定で無効。公開登録を本番の管理者作成手段にしない |
| `FORCE_HTTPS`, `HSTS_ENABLED`, `SESSION_COOKIE_SECURE` | 本番では有効に固定。ローカルHTTPの都合で本番設定を弱めない |
| `SESSION_COOKIE_SAMESITE` | `Lax`。CookieはHttpOnlyも有効 |
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | `5` / `900` |
| `REGISTER_RATE_LIMIT` / `REGISTER_RATE_WINDOW_SECONDS` | `3` / `3600` |
| `MAX_CONTENT_LENGTH` | `8388608`（8 MiB） |

### キュー・scheduler・ブラウザ

| 変数 | 既定値・扱い |
|---|---|
| `SCRAPE_QUEUE_BACKEND` | `inmemory`。本番splitは`rq` |
| `SCRAPE_QUEUE_NAME` | `scrape` |
| `MEDIA_QUEUE_NAME` | worker側で空ならscrape queueへフォールバック。Blueprintは`media`を明示 |
| `RQ_BURST` / `RQ_WITH_SCHEDULER` | 通常`false`。RQ schedulerと定期巡回用APSchedulerは別 |
| `WEB_SCHEDULER_MODE` | `auto`。本番splitは`disabled` |
| `WORKER_ENABLE_SCHEDULER` | `false`。定期処理ownerのworker 1台だけ`1` |
| `SCHEDULER_LOCK_BACKEND` | `auto`。inmemory互換はfile lock、worker / rqではRedis側のlockを使用 |
| `PATROL_BATCH_SIZE` | 通常`50`。全商品の15分以内の巡回完了を保証する値ではない |
| `WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP` | `true`。起動時に抽出ジョブの停滞整理・翻訳lease回復を実行する経路あり |
| `SCRAPE_JOB_HEARTBEAT_SECONDS` | `30` |
| `SCRAPE_JOB_STALL_TIMEOUT_SECONDS` / `SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS` | `900` / `60`。抽出ジョブの停滞・孤立判定用 |
| `WORKER_BACKLOG_WARN_COUNT` / `WORKER_BACKLOG_WARN_AGE_SECONDS` | `25` / `900`。起動時backlog診断のしきい値 |
| `WORKER_HEARTBEAT_ENABLED` | 一般設定は`false`、`worker.py`は有効を既定にする |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` / `WORKER_HEARTBEAT_TTL_SECONDS` | `15` / `90` |
| `WORKER_HEARTBEAT_KEY_PREFIX` | `esp:worker:heartbeat`。web / workerで一致させる |
| `WORKER_HEARTBEAT_FRESHNESS_SECONDS` | `60` |
| `SCHEDULER_HEARTBEAT_ENABLED` / `SCHEDULER_HEARTBEAT_KEY` | splitでは有効にし、共有keyは`esp:scheduler:heartbeat` |
| `SCHEDULER_HEARTBEAT_FRESHNESS_SECONDS` / `PATROL_HEARTBEAT_FRESHNESS_SECONDS` | 通常`1200` / `1200` |
| `ENABLE_SHARED_BROWSER_RUNTIME` / `WARM_BROWSER_POOL` | 一般設定では無効、worker entrypointでは有効を既定にする |
| `BROWSER_POOL_WARM_SITES` | `mercari` |
| `BROWSER_POOL_MAX_CONTEXTS` | 通常`1`。サイト別設定・各取得経路の並列性とは分けて考える |
| `BROWSER_POOL_MAX_TASKS_BEFORE_RESTART` / `BROWSER_POOL_MAX_RUNTIME_SECONDS` | 通常`0`。正の値で計画的recycleの上限を設定 |
| `MERCARI_USE_BROWSER_POOL_DETAIL` / `MERCARI_PATROL_USE_BROWSER_POOL` / `SNKRDUNK_USE_BROWSER_POOL_DYNAMIC` | worker entrypoint / Blueprintでは有効化する設定 |
| `RECORDCITY_BROWSER_PROFILE` | ローカルは`headless`、Blueprint workerは`persistent-chrome` |
| `RECORDCITY_FETCH_PROVIDER` | `browser`。`zyte` / `scraperapi` / `template`等の選択は明示設定と対応credentialが必要 |
| `SELECTOR_ALERT_WEBHOOK_URL` / `OPERATIONAL_ALERT_WEBHOOK_URL` | 未設定なら該当通知先なし。通知のcooldown / rate-limit設定も確認 |
| `WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP` | `false`。修復候補の自動処理は明示設定と検証条件が必要 |

サイト別の詳細並列数・タイムアウト・リトライや`{SITE}_BROWSER_POOL_*`は、対応する実装の変数名を確認して設定してください。すべてのサイトが同じ変数・取得機能を持つとは限りません。

### 翻訳・画像・メール

| 変数 | 既定値・扱い |
|---|---|
| `TRANSLATOR_BACKEND` | `argos`。Blueprintは`openai`。フォールバック用Argosも考慮 |
| `TRANSLATOR_SOURCE_LANG` / `TRANSLATOR_TARGET_LANG` | `ja` / `en` |
| `OPENAI_API_KEY` | OpenAI backend利用時に必要。該当プロセスのsecret環境変数で設定 |
| `BG_REMOVAL_BACKEND` | Blueprintは`rembg`。利用するweb / worker双方で揃える |
| `BG_REMOVAL_INTERNAL_SECRET` | workerからwebへの画像アップロード認証用。web / worker双方に同じsecretを設定し、Flaskの`SECRET_KEY`と混同しない |
| `ESP_WEB_INTERNAL_URL` / `WEB_INTERNAL_URL` / `WEB_PUBLIC_URL` | 画像処理workerのweb到達先上書き。未設定なら`WEB_INTERNAL_HOST` + portを使用 |
| `WEB_INTERNAL_HOST` / `WEB_INTERNAL_PORT` | Blueprintはwebのhostを注入し、内部portは`8080` |
| `U2NET_HOME` | Dockerでは`/opt/rembg`。モデル保存場所。ビルドの事前読込失敗はビルド失敗にならないため、推論確認が必要 |
| `IMAGE_STORAGE_PATH` | `static/images`。本番は永続ディスク上の`/var/data/images` |
| `IMPORT_PREVIEW_STORAGE_PATH` | 通常はFlask instance配下。実行ユーザーから書込み可能な保存先が必要 |
| `MAX_IMAGE_DOWNLOAD_BYTES` / `MAX_IMAGE_PIXELS` | `.env.example`では`5242880` / `20000000` |
| `ALLOWED_IMAGE_HOSTS` | 任意の外部画像host allowlist。URL・画像の検証を置き換える設定ではない |
| `MAIL_ENABLED` | `false`。credentialだけでは送信有効にならない |
| `MAIL_FROM` | `.env.example`は`noreply@jp-items.com`。送信providerで利用できる送信元を設定 |
| `RESEND_API_KEY` | メールを送るプロセスにのみ必要。Git・ログ・READMEに実値を書かない |
| `PORT` | DockerのGunicorn fallbackは`10000`。開発例では`flask run --port 5000`で明示 |

## 9. 使い方

### 抽出・編集・価格

`/scrape`で対応サイト、キーワードまたはURL、価格帯・件数などを指定します。結果を確認して必要な商品を登録し、商品一覧・詳細画面で画像、説明、翻訳、バリエーション、販売価格を編集します。プレビュー取得とDBへの登録は別の段階です。

価格ルールの基本式は`(仕入値 + 送料) × (1 + 利益率 / 100) + 固定費`です。最終表示・CSV出力の価格には、バリエーション別の明示販売価格や旧データのフォールバックも関係します。[共通価格解決処理](services/pricing_service.py)を参照してください。

### 翻訳・画像白抜き

翻訳提案と画像加工結果はジョブの状態を確認してから適用します。登録時の翻訳には自動適用経路もあります。画像白抜きでは、workerは商品画像を直接置換せず、webに結果を返し、利用者が適用または却下します。

`rq`はworkerが必要です。画像処理の`inmemory`互換経路はリクエスト内で同期実行されるため、常に非同期・即時応答になると仮定しないでください。モデル読込・推論・画像返送が成功するかを利用環境で確認してください。

### 公開カタログ

価格表管理で商品、レイアウト、テーマ、公開状態・期限を設定し、`/catalog/<token>`を共有します。公開ページでは検索、タグ絞り込み、Quick Viewを利用できます。公開期限切れ・非公開・所有者停止などの条件も表示可否に影響します。

公開レスポンスに内部仕入れ情報を含めない設計を維持してください。画像も公開用の自社media経路を使用します。カタログのリクエスト受付は決済ではなく、決済完了や在庫確保を表示・保証する機能として扱わないでください。

### メール設定と送信確認

```bash
# ローカルの設定だけを確認。通信・送信はしない
python -m flask mail-check

# 設定と入力のdry-run。--sendがない限り送信しない
# example.comの宛先は説明用。実送信時は指定された検証用宛先に置き換える
python -m flask mail-test --to recipient@example.com --delivery-key esp-mail-check-v1
```

`mail-check`は設定がreadyでない場合に終了コード2を返します。外部providerの認証や受信確認は行いません。実送信を行う場合にだけ`MAIL_ENABLED=true`と有効な送信設定を用意し、`mail-test`に`--send`を追加してください。同じテスト送信の再試行には同じdelivery keyを使います。送信APIの受付成功は受信者への到達確認ではありません。

## 10. 自動監視（パトロール）

[MonitorService](services/monitor_service.py)は、schedulerから通常15分間隔で呼ばれる**件数制限付きバッチ**です。scheduler起動後の初回実行もあります。

対象は、対応する7サイトの商品で、`is_listed=True`、未アーカイブ、未削除、かつ`next_patrol_at`の条件を満たすものです。未巡回・古い巡回順を優先し、通常は`PATROL_BATCH_SIZE=50`件まで処理します。Record City、商品リストのみの登録、まだ再試行時刻に達していない商品などは同じ扱いではありません。

失敗時の待機は**線形バックオフ**で、`min(15分 × 連続失敗回数, 180分)`です。指数バックオフではありません。対象が多い場合や失敗が続く場合、全体を1巡する時間は15分を超えます。

パトロールは取得結果に応じて商品の状態・価格とバリエーション在庫等を更新します。売切れ判定には信頼度・再確認の処理がありますが、**この更新処理自体は自動アーカイブや`ProductSnapshot`追加を行いません**。他の操作で作られる履歴・アーカイブと混同しないでください。

現在の在庫反映処理には、サイズ名の部分一致と、終端状態に矛盾するバリエーション在庫の扱いに改善事項があります。[横断レビュー](docs/REPOSITORY_REVIEW_2026-09-12.md)のP1項目を参照してください。自動更新だけを正確性の保証にせず、重要な在庫は取得元と照合してください。

## 11. CSV エクスポート

| 種別 | 用途 |
|---|---|
| Shopify商品登録CSV | タイトル・説明・分類・バリエーション・画像等の登録／更新 |
| Shopify在庫更新CSV | 在庫数の更新 |
| Shopify価格更新CSV | 販売価格の更新 |
| eBay File Exchange CSV | eBay向け一括出品データの作成 |

Shopify商品CSVの入口は`/export/shopify`、在庫更新は`/export_stock_update`、eBayは`/export_ebay`です。列・価格解決・画像URLの実際の仕様は[routes/export.py](routes/export.py)を確認してください。

出力したCSVは対応先で内容を確認して取り込みます。CSVを生成しただけで外部在庫が同期されたり、出品や決済が完了したりするものではありません。サイト側CSV仕様の変更も別途検証してください。

## 12. テスト

```bash
python -m pip install -r requirements-dev.txt
python -m pip check
python -m pytest -q

# 変更領域を絞る場合
python -m pytest tests/test_e2e_routes.py -q
python -m pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q
python -m pytest tests/test_monitor_service.py -q

# 除外なしの脆弱性監査。CIが除外している項目も見えるようにする
python -m pip_audit -r requirements.txt
```

[CI](.github/workflows/ci.yml)はPython 3.11で依存整合性、監査、pytest、本番セキュリティ設定のsmokeを実行します。現行CIの監査コマンドは次の例外付きです。

```bash
python -m pip_audit -r requirements.txt --ignore-vuln CVE-2026-54499
```

これはArgosが固定しているStanza依存に対する例外であり、脆弱性が存在しないという意味ではありません。例外の継続可否と依存更新方法は[横断レビュー](docs/REPOSITORY_REVIEW_2026-09-12.md)を参照してください。

[tests/conftest.py](tests/conftest.py)はSQLiteテストDBを構成します。通常のpytest成功を、そのまま本番PostgreSQL・Redis・RQの統合検証成功と読み替えないでください。テストによるセレクター設定の変更は一時ディレクトリへ隔離されています。

[Docker Build Validation](.github/workflows/docker-build.yml)はイメージのビルドに加えて、Patchrightのpersistent Chrome / Xvfb起動とSIGTERM時の終了処理を確認します。一方、現在のDocker smokeは画像白抜きの実推論やすべての外部サイトの到達性を確認するものではありません。

### DB・キュー・fixtureの確認

以下は環境・DBへアクセスする診断です。`--apply-migrations`やpersist系は書込みを伴います。破棄可能なローカルDB・キューを指定してから実行してください。

```bash
python -m flask db-smoke --require-backend postgresql --apply-migrations
python -m flask stack-smoke --require-backend postgresql --apply-migrations --mode persist
python -m flask local-verify --profile full --require-backend postgresql --apply-migrations
python -m flask worker-health --fail-on-warning
python -m flask schema-drift-check
```

`detail-fixture-smoke`、`search-fixture-smoke`は保存したHTMLの解析確認、`stack-smoke --fixture-site ...`はfixtureを利用したstack確認の入口です。fixtureごとの対応サイト・引数は各CLIの`--help`を参照してください。`scripts/live_search_acceptance.py`や手動のlive acceptance workflowは外部通信を伴い、通常の単体テストとは別です。既存の7サイト受入ツールを、Record Cityを含めた全8サイトの証明として扱わないでください。

## 13. ディレクトリ構成

責務別の抜粋です。モジュール数・テーブル数・テンプレート数を固定値として管理せず、実ファイルを参照してください。

```text
ESP/
├── app.py / wsgi.py             # Flask構成・WSGI・health・scheduler
├── worker.py                   # dedicated RQ worker
├── cli.py                      # 運用・診断CLI
├── database.py / models.py      # DB接続・bootstrap・モデル
├── security_config.py          # 本番セキュリティ設定
├── alembic/                    # schemaマイグレーション
├── requirements*.txt           # 実行・開発依存
├── Dockerfile / render.yaml    # コンテナ・本番split契約
├── docker-compose.local.yml    # ローカルPostgreSQL / Redis
├── *_db.py                     # Record Cityを含むサイト別取得入口
├── routes/                     # 商品・抽出・価格表・公開・認証・管理・翻訳・画像等
├── jobs/
│   ├── scrape_tasks.py
│   ├── translation_tasks.py
│   └── bg_removal_tasks.py
├── services/
│   ├── scrape_queue.py / queue_backend.py / scrape_job_store.py
│   ├── worker_runtime.py / browser_pool.py
│   ├── monitor_service.py / patrol/
│   ├── translator/ / bg_remover/ / media_queue.py
│   ├── product_service.py / pricing_service.py / image_service.py
│   ├── catalog_request_service.py / mail_service.py / mail_cli.py
│   └── repair_store.py / repair_worker.py / selector_healer.py
├── templates/ / static/        # UI・CSS・JavaScript・アセット
├── config/ / utils/            # 取得設定・共通処理
├── tests/ / .github/workflows/  # 回帰テスト・CI・運用workflow
├── docs/ / knowledge/          # 設計・runbook・運用記録
└── llama.cpp/                  # 別コンポーネント。明示指示なしに変更しない
```

## 14. 開発ロードマップ・現状ステータス

### 実装済みと未完了を分ける

RQ / DBへの抽出ジョブ記録、Record City抽出、翻訳レビューと回復、画像白抜きの投入・確認・適用／却下、公開カタログのタグ絞り込み、ロール管理、カタログリクエスト、Resend送信基盤は実装済みです。ただしモデル・認証情報・外部サービス・運用状態の確認は別途必要です。

背景処理とジョブ永続化を一括して「未実装」とする旧記述は更新しました。タグによる絞り込みがあることと、独立したカテゴリ体系・階層分類の仕様が完成していることも別です。PayPal等の決済連携、在庫予約、注文確定は、受付機能とは分けて仕様確認してください。

### 優先する改善領域

在庫反映の正確性、画像ジョブの中断回復・競合制御、依存監査例外、PostgreSQL / Redisを使う統合テスト、モデルの実推論smoke、文書とサイト能力定義の同期が改善候補です。根拠・再現条件・受入テスト案は[2026-09-12横断レビュー](docs/REPOSITORY_REVIEW_2026-09-12.md)にまとめています。

過去の計画は[UNIFIED_ROADMAP](docs/UNIFIED_ROADMAP.md)を参照してください。過去のStage完了やタスク記録は、その後に変更された現在の機能・Docker構成を保証するものではありません。

## 15. 運用上の注意事項

### readinessと業務状態は分けて見る

Blueprintのhealth checkは`/readyz`です。これはweb自身に必要なDB・Redisなどの到達性確認です。worker heartbeat、worker所有scheduler、最近のpatrol完了を含む運用確認は`/stack-readyz`を使用し、`/healthz`の最小情報と混同しないでください。webが応答するだけでは、キューが消化されている証明にはなりません。

### 現行splitのCLI入口

| 用途 | CLI |
|---|---|
| 設定・schema監査 | `predeploy-check --target split-render --strict`, `schema-drift-check` |
| Blueprint・入力値監査 | `render-blueprint-audit`, `render-dashboard-inputs`, `render-budget-guardrail-audit --blueprint-path render.yaml` |
| ローカルrehearsal | `render-local-split-checklist`, `render-local-split-readiness` |
| split readiness | `render-cutover-readiness --require-backend postgresql --apply-migrations --strict` |
| operator向け手順 | `render-cutover-brief`, `render-cutover-checklist`, `render-worker-postdeploy-checklist --blueprint-path render.yaml` |
| deploy後の確認 | `render-postdeploy-smoke --base-url https://YOUR-WEB-HOST`, `worker-health --fail-on-warning` |

各コマンドは`python -m flask`に続けて実行します。追加引数・副作用は`--help`と[runbook](docs/RENDER_CUTOVER_RUNBOOK.md)で確認してください。`--apply-migrations`はDB変更、認証付きsmokeや`--ensure-user`は認証・登録操作を伴い得ます。smoke用パスワードを共有ログやシェル履歴へ残さないよう管理してください。

費用監査CLIの金額はリポジトリ側のplanning assumptionです。実際の契約・サービス増設前に料金とリソース条件を別途確認してください。

### legacy single-webは別の契約

`single-web-redeploy-readiness`、`single-web-redeploy-checklist`、`single-web-postdeploy-smoke`、`single-web-smoke`、`predeploy-check --target single-web`は互換確認用です。現行splitの実行手順と混ぜないでください。

inmemory実行中はGunicornの複数プロセス化やプロセス再起動で実行管理が分断・消失します。`--workers 1` / `--max-requests 0`という互換運用上の注意はこの事情によるもので、RQの耐久性やすべてのジョブの中断回復を保証する説明ではありません。

### 永続データ・secret・外部サービス

DB、画像・ロゴ、処理結果、必要なモデル資産の保存先とバックアップ・復旧手順を確認してください。コンテナ再作成で消えるfilesystemに依存せず、workerの停止時はキュー・DB状態・heartbeatも確認します。schema変更ではwebだけでなくworkerの起動失敗も確認してください。

モデルのpreload成功・providerのAPI受付・CI成功は、それぞれ実推論・メール到達・本番業務全体の成功と同じではありません。外部サイト、外部API、認証情報、商品データを利用する検証は明示的に範囲を定めて実施してください。

## ライセンス

このプロジェクトのライセンス条件については、リポジトリオーナーにお問い合わせください。
