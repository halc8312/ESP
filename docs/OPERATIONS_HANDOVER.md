# ESP 運用引き継ぎ・バックアップ／復元手順

2026-09-30作成。これは手順書であり、本番バックアップ、復元試験、アカウント移管、支払変更、著作権の合意を実施した記録ではない。保存先・実施担当・保存期間・復旧目標時間（RTO）・許容するデータ損失時間（RPO）は未確定。

## 現在の構成

2026-09-29の反映確認で使用した構成を記載する。実行前に同じRenderワークスペースとサービスIDを確認する。Blueprint上の論理名`esp-web`と、既存の実サービス名`ESP-1`は異なる。

| 対象 | 現在の識別情報・役割 |
| --- | --- |
| GitHub | `halc8312/ESP`。webとworkerへ同じ検証済みコミットを反映 |
| Render workspace | `tea-d4nfak0gjchc73c3ensg` |
| web | `ESP-1` / `srv-d4nn33khg0os739gc1f0`。HTTP・公開カタログ・画像保存 |
| worker | `esp-worker` / `srv-d77fj5p5pdvs7399cmh0`。`tini -- python worker.py`、RQ・巡回・定期処理 |
| PostgreSQL | `esp-postgres` / `dpg-d77finp5pdvs7399cjt0-a`。本番メジャーバージョン18 |
| Redis/Valkey | 既存`esp-keyvalue`。共有キュー・ロック・heartbeat・アクセス制御。サービスIDは引き継ぎ時に確認 |
| 画像 | webの既存10GBディスク、マウント`/var/data`、`IMAGE_STORAGE_PATH=/var/data/images` |

`render.yaml`は分離構成の参照で、既存サービスを新規作成する指示ではない。`render.existing-web-addons.yaml`、`docs/RENDER_CUTOVER_RUNBOOK.md`、`docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md`には移行前の記述が残る。この運用では「分離構成が未稼働」「キューをinmemoryへ変更する」を適用しない。既存webの作り直し、DBの作り直し、ディスクの置き換えは通常の反映手順に含めない。

## 引き継ぐ設定名

以下はソースコード・設定ファイルで使われる**名前の台帳**であり、現在の本番設定値を全件取得した証拠ではない。実値とSecretは、権限を持つ担当者が秘密情報の管理場所へ引き継ぐ。DB接続URL、APIキー、Webhook URL、パスワード、Cookie、期限付き共有URLをこの文書・git・PR・端末の出力へ書かない。

| 分類 | 環境変数名 | 確認すること |
| --- | --- | --- |
| 共通接続・認証 | `APP_ENV`, `RUNTIME_ROLE`, `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `VALKEY_URL` | 本番で明示的なSecretを使用。web/workerの`SECRET_KEY`・DB・Redisを一致させる |
| キュー | `SCRAPE_QUEUE_BACKEND`, `SCRAPE_QUEUE_NAME`, `MEDIA_QUEUE_NAME`, `SCRAPE_JOB_TIMEOUT_SECONDS`, `SCRAPE_JOB_FAILURE_TTL_SECONDS` | 分離構成は`rq`。キュー名を両サービスで一致させる |
| schema | `SCHEMA_BOOTSTRAP_MODE` | releaseのAlembic revisionとDBの`alembic_version`を確認。復元検証の初回起動では`disabled` |
| web・画像 | `PORT`, `INTERNAL_PORT`, `IMAGE_STORAGE_PATH`, `MAX_CONTENT_LENGTH`, `MAX_IMAGE_DOWNLOAD_BYTES`, `MAX_IMAGE_PIXELS`, `ALLOWED_IMAGE_HOSTS`, `SCRAPE_IMAGE_CACHE_ENABLED` | 画像実体をwebの永続ディスクから回収。workerの一時領域だけをバックアップしない |
| worker→web | `WEB_INTERNAL_HOST`, `WEB_INTERNAL_PORT`, `WEB_INTERNAL_URL`, `WEB_PUBLIC_URL`, `BG_REMOVAL_INTERNAL_SECRET` | 実際の既存webへ接続。論理名だけからホスト名を推定しない |
| 定期処理 | `WEB_SCHEDULER_MODE`, `WORKER_ENABLE_SCHEDULER`, `SCHEDULER_LOCK_BACKEND`, `SCHEDULER_LOCK_KEY`, `SCHEDULER_LOCK_TTL_SECONDS`, `SCHEDULER_LOCK_RETRY_ENABLED`, `SCHEDULER_LOCK_RETRY_SECONDS`, `SCHEDULER_LOCK_RETRY_MAX_SECONDS`, `PATROL_BATCH_SIZE` | web側は無効、定期処理を所有するworkerを一つにする |
| heartbeat | `WORKER_HEARTBEAT_ENABLED`, `WORKER_HEARTBEAT_KEY_PREFIX`, `WORKER_HEARTBEAT_INTERVAL_SECONDS`, `WORKER_HEARTBEAT_TTL_SECONDS`, `WORKER_HEARTBEAT_FRESHNESS_SECONDS`, `SCHEDULER_HEARTBEAT_ENABLED`, `SCHEDULER_HEARTBEAT_KEY`, `SCHEDULER_HEARTBEAT_FRESHNESS_SECONDS`, `PATROL_HEARTBEAT_FRESHNESS_SECONDS` | 再起動後に新しい実行プロセスのheartbeatと巡回完了を確認 |
| browser | `WARM_BROWSER_POOL`, `ENABLE_SHARED_BROWSER_RUNTIME`, `BROWSER_POOL_WARM_SITES`, `MERCARI_USE_BROWSER_POOL_DETAIL`, `MERCARI_PATROL_USE_BROWSER_POOL`, `SNKRDUNK_USE_BROWSER_POOL_DYNAMIC`, `RECORDCITY_BROWSER_PROFILE`, `RECORDCITY_FETCH_PROVIDER` | RecordCityの専用Chrome/Xvfb経路と、他サイトのheadless経路を維持 |
| 取得上限・2段階取得 | `RECORDCITY_LISTING_ENABLED`, `SCRAPE_MAX_ACTIVE_JOBS_PER_USER`, `SCRAPE_JOB_HEARTBEAT_SECONDS`, `SCRAPE_JOB_STALL_TIMEOUT_SECONDS`, `SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS` | 新しい一覧取得は機能フラグと実HTMLによる受け入れ確認を経て有効化。実装済みを本番有効と扱わない |
| 翻訳・画像加工 | `TRANSLATOR_BACKEND`, `OPENAI_API_KEY`, `OPENAI_TRANSLATOR_MODEL`, `OPENAI_BASE_URL`, `TRANSLATOR_SOURCE_LANG`, `TRANSLATOR_TARGET_LANG`, `OPENAI_TRANSLATOR_TIMEOUT_SECONDS`, `OPENAI_TRANSLATOR_MAX_RETRIES`, `OPENAI_TRANSLATOR_MAX_OUTPUT_TOKENS`, `BG_REMOVAL_BACKEND`, `BG_REMOVAL_MODEL`, `BG_REMOVAL_MAX_INPUT_DIMENSION` | 課金・外部接続がある。復元試験へ本番のAPIキーを持ち込まない |
| 通知 | `MAIL_ENABLED`, `MAIL_PROVIDER`, `CATALOG_REQUEST_NOTIFICATIONS_ENABLED`, `MAIL_FROM`, `RESEND_API_KEY`, `SELECTOR_ALERT_WEBHOOK_URL`, `OPERATIONAL_ALERT_WEBHOOK_URL`, `SCRAPE_ALERT_WEBHOOK_URL` | メールは送信と依頼通知の両フラグが必要。`MAIL_PROVIDER=resend`を明示。API受理は受信の証拠ではない |
| worker回復・監視 | `WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP`, `WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP`, `WORKER_SELECTOR_REPAIR_LIMIT`, `WORKER_BACKLOG_WARN_COUNT`, `WORKER_BACKLOG_WARN_AGE_SECONDS`, `SELECTOR_REPAIR_MIN_SCORE`, `SELECTOR_REPAIR_MIN_CANARIES`, `SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL`, `SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL`, `SELECTOR_REPAIR_STORE_MODE` | 起動時の変更・再実行と、外部canary接続の有無を確認 |
| 任意の外部取得 | `RECORDCITY_ZYTE_API_KEY`, `RECORDCITY_SCRAPERAPI_KEY`, `RECORDCITY_SCRAPERAPI_ROUTING`, `RECORDCITY_FETCH_API_URL_TEMPLATE`, `RECORDCITY_PROXY_URL`, `SURUGAYA_ZYTE_API_KEY`, `SURUGAYA_SCRAPERAPI_KEY`, `SURUGAYA_FETCH_API_URL_TEMPLATE`, `SURUGAYA_PROXY_URL` | 使っているものだけを秘密台帳へ記載。診断用の設定を本番取得へ自動転用しない |
| 認証・HTTPS | `ALLOW_PUBLIC_SIGNUP`, `LOGIN_RATE_LIMIT`, `LOGIN_RATE_WINDOW_SECONDS`, `REGISTER_RATE_LIMIT`, `REGISTER_RATE_WINDOW_SECONDS`, `FORCE_HTTPS`, `HSTS_ENABLED`, `HSTS_MAX_AGE`, `HSTS_INCLUDE_SUBDOMAINS`, `HSTS_PRELOAD`, `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE` | 本番の認可と利用者分離を維持。ローカル試験は別Secret・別セッション |

サイト別アクセス間隔などの可変キーは`services/marketplace_access.py`、browserのサイト別キーは`services/browser_pool.py`を併せて確認する。環境変数を丸ごと`env`/`printenv`で表示・共有しない。

## バックアップの単位

一組として保存するものは、PostgreSQLのcustom-format dump、`images/`の画像実体、検証済みreleaseのGit SHA、Alembic revision、取得開始・終了時刻（UTC）、画像件数・サイズ・SHA-256、復元試験結果。Secretの保管は別の秘密台帳とする。

DBには利用者、商品、価格表、依頼、ジョブと通知の送信状態が含まれる。公開カタログだけのCSVは代替にならない。画像はローカルキャッシュ・アップロード・ロゴを含む`/var/data/images`全体を対象にし、DB内の参照だけで済ませない。

Redisの古いRQジョブ、heartbeat、ロックは、DBと同じ時点の永続業務記録とはみなさない。復元試験では空のRedisを作る。災害復旧時の未完了ジョブはDBの状態を確認して必要なものだけを再登録する。古いRedis snapshotを戻して、メール・巡回・画像取得をまとめて再実行しない。

DBの`pg_dump`はDB内のスナップショットで、別取得の画像アーカイブとの同時点を保証しない。整合する一組が必要な取得では、運用担当がメンテナンス時間を確保し、新規入力・画像更新・workerの書き込みを止め、実行中処理の終了を確認してから両方を保存する。稼働中に別々に取得したものは、その事実と時刻差を記録し、「整合確認済み」としない。

## 保存コマンドのひな型（未実行）

PG18の`pg_dump`/`pg_restore`があり、承認されたDB接続経路で実行できる管理環境を使う。通常のESPコンテナにPostgreSQL CLIがあるとは仮定しない。DB接続のために外部IP許可範囲を広げる操作は、この手順に含めない。

接続は保護されたlibpq service file（`esp_backup`）とpassfileを事前設定し、権限を`0600`にする。接続URLを`--dbname`へ渡したり、パスワードをコマンド引数へ渡したりしない。`set -x`を使わない。出力ディレクトリは新規作成し、既存バックアップを上書きしない。

```bash
umask 077
# ESP_BACKUP_DIR: 新しいバックアップ一組を置く非公開ディレクトリ。
# ESP_RELEASE_SHA: 稼働中web/workerで確認した40文字のコミットSHA。
mkdir "$ESP_BACKUP_DIR"
pg_dump --version
PGSERVICE=esp_backup pg_dump --format=custom --no-owner --no-privileges \
  --file "$ESP_BACKUP_DIR/database.dump"
# listにはschema名などが含まれるので、公開ログへ出さず私的な記録へ保存。
pg_restore --list "$ESP_BACKUP_DIR/database.dump" > "$ESP_BACKUP_DIR/database.toc"
```

画像取得は**既存webのディスクを読める環境**で次の形で行う。`ESP_BACKUP_DIR`はその環境で作成した非公開の新しい場所とする。実サービスへの接続・アーカイブの転送方式は担当者が権限を確認して決め、転送後にSHA-256を照合する。

```bash
tar --create --gzip --file "$ESP_BACKUP_DIR/images.tar.gz" \
  --directory /var/data images
```

同じ一組をローカルへ安全に回収した後、次のコマンドはネットワークへ接続せず、DBやファイルを復元せず、ファイル名やアーカイブ内の名前を出力しない。

```bash
python scripts/backup_restore_plan.py \
  --dump "$ESP_BACKUP_DIR/database.dump" \
  --media "$ESP_BACKUP_DIR/images.tar.gz" \
  --revision "$ESP_RELEASE_SHA" > "$ESP_BACKUP_DIR/manifest.json"
```

この検査が行うのは、DB dumpの`PGDMP`ヘッダ、画像アーカイブの相対パス・通常ファイル／ディレクトリ、サイズとSHA-256の検査だけ。symlink、hardlink、特殊ファイル、パス遡行、重複パスは拒否する。DBが復元可能か、画像参照がそろうか、DBと画像が同じ時点かは証明しない。変更されないバックアップ一組を対象にする。暗号化・アクセス権・別保管先への保存・定期取得はこの検査CLIには含まない。

## 隔離した復元リハーサル（明示的に実行する場合のみ）

以下のrestoreは書き込み操作であり、オフライン検査CLIから自動実行されない。本番・共有開発DB・既存の画像ディレクトリを対象にしない。作業対象をローカルの新規コンテナと空DBに限定し、dumpとreleaseを取得した時点の組み合わせで検証する。`docker-compose.local.yml`はPG16を使うため、PG18の復元試験の代わりにしない。

1. 新しい作業ディレクトリを作り、manifestのSHA-256と回収した実体を照合する。機微な実データを置くためアクセス権を限定する。既存ファイル・サービス・volumeを削除して空にする方法は使わない。
2. 新しいDocker internal networkを作る。DBとRedisはホストへポートを公開しない。同名のnetwork/containerがあると作成は失敗するので、既存のものを再利用せず名前を変更する。
3. PG18・空のRedis・空のmediaを作り、DBがPG18かつ空であることを確認する。復元にはPG18の`pg_restore`を使う。
4. 通常のweb/worker起動より先にDBと画像を復元し、SQLで件数・revision・参照を確認する。ここでは本番のworkerを起動しない。
5. 同じGit SHAのアプリをローカルで確認する場合も、外部通信をネットワークで遮断し、メール・scheduler・外部Webhookを無効にした別設定を使う。既存の通知を自動再送しない。

以下は使い捨てのローカル環境専用の例。`ESP_REHEARSAL_DIR`は新しい絶対パスとする。PostgreSQLの秘密はファイルに保存し、引数・端末出力へ出さない。復元ファイルをDocker socketや本番のマウントへ接続しない。

同じshellで次のwrapperを定義し、この節のDocker操作にはすべて使用する。`env -i`で`DOCKER_HOST`・`DOCKER_CONTEXT`・TLS設定などの継承環境を取り除き、ローカルUnix socketを明示する。ローカルdaemonが利用できない場合は停止し、remote contextへ切り替えない。

```bash
set -eu
umask 077
esp_restore_docker() {
  env -i PATH="$PATH" LANG=C.UTF-8 \
    docker --host=unix:///var/run/docker.sock "$@"
}
mkdir "$ESP_REHEARSAL_DIR"
mkdir "$ESP_REHEARSAL_DIR/media"
openssl rand -hex 32 > "$ESP_REHEARSAL_DIR/pg-password"
esp_restore_docker network create --internal esp-restore-isolated
esp_restore_docker run -d --name esp-restore-pg18 --network esp-restore-isolated \
  --env POSTGRES_DB=esp_restore --env POSTGRES_USER=esp_restore \
  --env POSTGRES_PASSWORD_FILE=/run/secrets/pg-password \
  --mount "type=bind,src=$ESP_REHEARSAL_DIR/pg-password,dst=/run/secrets/pg-password,readonly" \
  postgres:18
esp_restore_docker run -d --name esp-restore-redis --network esp-restore-isolated \
  redis:7-alpine redis-server --save '' --appendonly no --maxmemory-policy noeviction
```

PGの起動完了を確認後、対象は新しいローカルPGコンテナ内のUnix socketだけに固定する。次の検査が通らなければrestoreへ進まない。DBオブジェクトがある場合の`--clean`、DROP、TRUNCATE、cascadeによる上書きは行わない。

```bash
test "$(esp_restore_docker exec esp-restore-pg18 psql -X --no-psqlrc \
  --host=/var/run/postgresql --username=esp_restore --dbname=esp_restore \
  --tuples-only --no-align --set=ON_ERROR_STOP=1 \
  --command="SELECT CASE WHEN current_setting('server_version_num')::int BETWEEN 180000 AND 189999 AND current_database() = 'esp_restore' AND NOT EXISTS (SELECT 1 FROM pg_depend d JOIN pg_namespace n ON d.refclassid='pg_namespace'::regclass AND n.oid=d.refobjid WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_') AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public','pg_catalog','information_schema') AND nspname !~ '^pg_') THEN 'safe_empty_local_pg18' ELSE 'refuse_restore' END;")" = safe_empty_local_pg18
```

対象・空DB・SHA-256の確認を終えた担当者が、**復元を実施すると決めた場合のみ**以下を実行する。DB restoreはエラー時に全体をロールバックする。画像の復元先も空であることを確認し、既存ファイルを上書きしない。

```bash
esp_restore_docker exec -i esp-restore-pg18 pg_restore \
  --host=/var/run/postgresql --username=esp_restore --dbname=esp_restore \
  --no-owner --no-privileges --exit-on-error --single-transaction \
  < "$ESP_BACKUP_DIR/database.dump"
test -z "$(find "$ESP_REHEARSAL_DIR/media" -mindepth 1 -print -quit)"
tar --extract --gzip --file "$ESP_BACKUP_DIR/images.tar.gz" \
  --directory "$ESP_REHEARSAL_DIR/media" \
  --no-same-owner --no-same-permissions --keep-old-files
```

例の空DB検査とrestoreは二つの別コマンドなので、その間に他プロセスを接続させない。失敗したDBへ繰り返しrestoreせず、新しい隔離環境で原因を確認する。archiveの検査後に実体を変更・差し替えない。

### アプリ確認用の隔離設定

PG18・Redis・アプリを同じ新規internal networkに置き、アプリの公開ポートは必要な場合だけ`127.0.0.1`へbindする。インターネットへ出られるホスト上で通常の`flask`/`worker.py`を起動するだけでは、外部接続の隔離にならない。イメージ・依存関係は実データを接続する前に用意する。

新しいSecretとローカル接続だけを含む保護されたenv fileを作り、`--env-file`で渡す。本番のenv group・DB URL・Redis URL・APIキー・Webhookをコピーしない。

| 設定 | 復元試験での扱い |
| --- | --- |
| `APP_ENV`, `RUNTIME_ROLE` | ローカル用設定、確認するwebだけを起動 |
| `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY` | 隔離PG18・空Redis・新規Secret。接続値はenv fileだけに記載 |
| `IMAGE_STORAGE_PATH` | 復元した`media/images`をアプリへ接続。公開閲覧の確認時はread-only mount |
| `SCHEMA_BOOTSTRAP_MODE` | `disabled`。dumpと同じreleaseで初回確認し、schemaを自動更新しない |
| `WEB_SCHEDULER_MODE`, `WORKER_ENABLE_SCHEDULER`, `RQ_WITH_SCHEDULER` | `disabled`, `0`, `0` |
| `MAIL_ENABLED`, `CATALOG_REQUEST_NOTIFICATIONS_ENABLED` | 両方`false` |
| `RESEND_API_KEY`, `OPENAI_API_KEY`, 外部取得キー、Webhook | 未設定。SMTPへの代替送信を設定しない |
| `WARM_BROWSER_POOL`, `WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP` | `0`。worker自体を起動しない |
| `RECORDCITY_LISTING_ENABLED` | 初回検証では`false`。外部取得は試験に含めない |

`/readyz`ではローカルDB/Redis接続を確認する。workerを意図的に起動しないため、`/stack-readyz`のworker・scheduler・patrol未観測はこの試験では想定内であり、本番の正常確認に置き換えない。

### 合格記録

- PostgreSQL major、`alembic_version`、release SHA、dump/mediaのSHA-256、実行担当・開始終了時刻と経過時間を記録する。
- SQLまたは非公開の管理画面で利用者・店舗・商品・価格表・依頼の件数を照合し、所有者の関連が維持されていることを確認する。顧客情報やメール本文を公開ログへ出さない。
- ローカル画像参照が復元した実体へ解決し、代表的なカタログ画像・ロゴ・アップロード画像を表示できることを確認する。外部CDN URLだけの画像は復元実体に含まれない。
- 公開カタログに`source_url`/`site`が出ないこと、別利用者の管理データへアクセスできないことを確認する。
- outboxの`accepted`を配達済みと扱わず、過去の待機・実行中・結果不明通知を実送信せずに確認する。復旧時のメール再試行は、凍結した本文・宛先と期限を確認して別途判断する。
- 実際に経過時間を測るまで「すぐ復元できる」「RTO達成」と報告しない。試験が失敗した場合も原因・影響範囲を記録する。

## 通常の反映・障害時の確認

反映は、検証済みコミット、Alembic変更と互換性、web/workerの同一SHA、画像ディスク維持を確認して行う。DB schemaが進んだ後は、旧コードがそのschemaで起動できるかを先に検証し、コードの巻き戻しだけで安全と判断しない。通常の障害対応としてDBを逆マイグレーションしたり、古いdumpを本番へ上書きしたりしない。

本番の確認では、Renderのlive表示に加え、`/readyz`のDB/Redis、`/stack-readyz`のworker・scheduler・巡回完了、workerのRQ待受と直近巡回の成功／失敗数、停滞ジョブ、画像表示を確認する。HTTP接続が確認環境のタイムアウトで失敗した場合は「直接確認未完了」と記録し、本番の障害とも正常とも断定しない。

メールは既定で無効。`flask catalog-notification-status`は状態別件数を読むだけで送信しない。無効時に作成した過去依頼を有効化後に自動で通知し直さない。送信元または現在の所有者メールが変わった通知は、別宛先へ転送せず取消として確認する。実送信・受信確認には指定されたテスト宛先が必要で、APIキーを設定したことや`accepted`だけでは完了としない。詳細は`docs/RESEND_MAIL_RUNBOOK.md`を参照する。

## 引き継ぎ時に埋める記録

| 項目 | 状態 |
| --- | --- |
| 管理者・予備の対応者・連絡手段 | 未確認 |
| GitHub/Render/DB/画像/Resend/ドメインDNSの権限 | 移管・権限付与は未実施。担当とアクセス回復方法を確認する |
| Secretの保存先・更新担当 | 未確定。実値をこの文書に記載しない |
| バックアップ保存先・暗号化・保存期間・定期取得担当 | 未確定 |
| 最新の整合したバックアップ日時・SHA-256 | 未取得・未記録 |
| 最新復元リハーサル日時・所要時間・合格記録 | 未実施 |
| RPO / RTO / 障害連絡の対応時間 | 未合意 |
| メールのテスト宛先・API受理・受信確認 | 未確認 |
| Render料金・支払者、所有権・サポート契約 | この技術手順書で合意・変更していない |

この記録を埋め、別の担当者が隔離環境で復元を再現できたことを確認して、運用引き継ぎ完了とする。

## 架空データによるPG18復元CI

`.github/workflows/restore-rehearsal.yml`は、ジョブ専用の`postgres:18`と空のRedisを使う。本番のSecretやURLを受け取らず、`scripts/restore_rehearsal.py --execute`で二つの異なるランダム名のDBを新規作成する。既存のDBをclean/dropする操作はなく、復元先の空状態を確認してからPG18コンテナ内のUnix socketで`pg_dump`/`pg_restore`を実行する。

二人の架空の所有者、それぞれの店舗・商品・画像・価格表・依頼・thumbnail状態・通知outbox（accepted/pending）をAlembic `20260930_0026`で作成し、dump前後の業務行のSHA-256と件数を照合する。復元したアプリの`/readyz`、管理画像、所有者の参照、別所有者へのアクセス拒否、公開ページの取得元非公開を検査する。メール送信・巡回・schedulerを無効にし、PythonからのDNS/ネットワーク接続は指定のloopback DB/Redisポートだけに制限する。

ローカルの安全条件・架空データ準備のテストは次の形で行える。これはPG18でのdump／restore実行の代替にはならない。

```bash
python -m pytest tests/test_backup_restore_plan.py tests/test_restore_rehearsal.py -q
# 既定動作はネットワークもDB作成も行わない。
python scripts/restore_rehearsal.py
```

この文書を作成した作業環境にはDocker／PostgreSQL CLIがなく、PG18復元CIはまだ実行していない。CIのrun URL、対象Git SHA、成功／失敗結果を確認して別途記録する。CIで架空データの復元が成功しても、本番バックアップの取得、実データの復元試験、移管完了、RPO/RTO達成の証拠とはしない。
