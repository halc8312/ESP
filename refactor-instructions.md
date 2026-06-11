# refactor-instructions.md — ESP リファクタリング実装指示書

このドキュメントは halc8312/ESP リポジトリのリファクタリングを実装担当モデル（Codex / Opus 等）が完遂するための指示書である。
2026-06-11 時点の `main`（commit `70ba969`）のコードベース全体を読み込んだうえで作成した。

---

## 1. Objective

既存仕様・既存挙動を一切壊さずに、以下を達成する。

1. リポジトリのルートに散乱した一回限りのデバッグ／検証スクリプトを整理し、本番コードと運用コードの境界を明確にする
2. サイト別スクレイパー（`mercari_db.py` 等7ファイル）の明白な重複と防御的 dead code を削減する
3. DB セッション管理とスキーマ管理の二重化された仕組みの境界を明確化し、誤用を防ぐ
4. ログ・エラーハンドリングの不統一を是正する
5. 上記により、今後の機能追加（AGENTS.md の "Not yet implemented" 項目）を安全に行える状態にする

**目的は見た目の綺麗さではない。** 「既存仕様を壊さず、負債を減らし、変更しやすくする」ことだけが目的である。
証拠なく大きな削除・全面書き換えをしてはならない。

---

## 2. Project Understanding

### 何をするものか

ESP は日本のEC・フリマサイト（メルカリ / ラクマ / Yahoo!ショッピング / ヤフオク / 駿河屋 / オフモール / SNKRDUNK の7サイト）から商品情報をスクレイピングし、価格計算 → Shopify/eBay 向け CSV エクスポート → 顧客向け公開価格表（カタログ）→ 15分ごとのパトロール（価格・在庫監視）までを管理する Flask 製マルチユーザー Web アプリ。

### デプロイ構成（AGENTS.md の契約 — 厳守）

- 本番 Render は split topology: `esp-web`（web）+ `esp-worker`（RQ worker）+ `esp-keyvalue`（Redis/Valkey）+ `esp-postgres`（PostgreSQL）
- `render.yaml` がこの契約の参照元。single-web 系のコマンド・runbook は legacy 互換用として**意図的に残されている**
- `worker.py` が split worker 専用エントリーポイント。scheduler owner は worker 側（`WORKER_ENABLE_SCHEDULER=1` は1台のみ）

### エントリーポイント

| ファイル | 役割 |
|---|---|
| `wsgi.py` → `app.py: create_app(runtime_role)` | Web。`RUNTIME_DEFAULTS` dict で role 別（base/web/cli/worker/test）の起動挙動を切替 |
| `worker.py` → `services/worker_runtime.py: run_worker` | RQ worker。browser pool warm、schema bootstrap、stalled job 掃除を起動時に実施 |
| `cli.py: register_cli_commands`（3,624行） | 約30個の Flask CLI コマンド（db-smoke, stack-smoke, render-cutover-readiness, single-web-* 等の運用ゲート群） |

### 主要モジュールと責務

- `models.py`（470行）: 全15モデル。User / Shop / Product / Variant / ProductSnapshot / PricingRule / PriceList / PriceListItem / CatalogPageView / ScrapeJob / ScrapeJobEvent / SelectorRepairCandidate / SelectorActiveRuleSet / TranslationSuggestion / ImageProcessingJob
- `database.py`（402行）: engine 生成、`SessionLocal`（scoped_session）と `create_isolated_session()`、Alembic bootstrap、`ADDITIVE_STARTUP_MIGRATIONS`（生SQLの追加カラムパッチセット）、drift 検査、db smoke
- `routes/`（16 blueprint）: main（一覧）/ products（編集）/ scrape / export / api / auth / shops / pricing / pricelist / catalog（公開・ログイン不要）/ archive / trash / settings / import_routes / translation / bg_removal
- `services/`（約40モジュール）: scrape_queue（inmemory）、queue_backend（rq/inmemory 切替）、scrape_job_store（durable job state）、worker_runtime、browser_pool / browser_runtime（共有 Playwright）、selector_healer + repair_store + repair_worker（セルフヒーリング CSS セレクター）、patrol/（7サイト分）、translator/（argos / openai バックエンド + suggestion_store）、bg_remover/（rembg + HMAC internal upload）、pricing_service、product_service、image_service、monitor_service、alerts
- ルート直下のサイト別スクレイパー: `mercari_db.py`(1,234) `surugaya_db.py`(1,249) `snkrdunk_db.py`(742) `yahoo_db.py`(505) `yahuoku_db.py`(388) `rakuma_db.py`(230) `offmall_db.py`(401)
- `jobs/`: RQ タスク定義（scrape_tasks / translation_tasks / bg_removal_tasks）
- `llama.cpp/`: 同梱サブツリー。**触らない**

### データフロー（抽出）

UI（`routes/scrape.py` + `static/js/scrape_form.js`）→ `services/scrape_request.py` → queue（inmemory or RQ）→ `jobs/scrape_tasks.py` → サイト別 `*_db.py`（Scrapling HTTP / Playwright browser pool）→ プレビュー → 登録（`services/product_service.py`：Product/Variant/Snapshot 保存、登録時翻訳ジョブ enqueue + デフォルト価格ルール適用）。job 状態は `scrape_job_store`（DB durable）+ Redis heartbeat で追跡。

### 外部依存

7つの対象ECサイト（スクレイピング）、Redis/Valkey、PostgreSQL/SQLite、Argos Translate（ローカル）、OpenAI API（翻訳バックエンド）、rembg、webhook 通知（`SELECTOR_ALERT_WEBHOOK_URL` / `OPERATIONAL_ALERT_WEBHOOK_URL`）、Render。

### 現在の検証コマンド（baseline 実績）

```bash
python -m pytest -q          # 2026-06-11 main にて: 698 passed, 1 skipped, ~100s
pytest tests/test_e2e_routes.py -q                                  # UI/ルート変更時
pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q  # worker/runtime 変更時
flask single-web-redeploy-readiness                                 # legacy 互換ゲート
flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict  # split 構成ゲート
```

- CI（`.github/workflows/ci.yml`）: `pip check` → `pip-audit` → `pytest -q` → 本番セキュリティ設定 smoke
- lint / typecheck コマンドは**存在しない**（ruff / mypy 等未導入）
- `pytest.ini` の `testpaths = tests` により、ルート直下の `test_*.py` は**収集されない**

---

## 3. Behaviors To Preserve（絶対に壊してはいけない挙動）

1. **公開カタログ（`routes/catalog.py` / `templates/catalog.html`）に `source_url` / `site` 等の内部仕入れ情報を絶対に出さない**
2. **ユーザー分離・ショップ分離・価格表分離**（routes 全体で `user_id == current_user.id` フィルタが約35箇所。1つでも欠けるとデータ漏洩）
3. **Render の web / worker / database / queue の env 契約**（`render.yaml`、`DATABASE_URL` / `REDIS_URL` / `SECRET_KEY` / `SCRAPE_QUEUE_BACKEND` の共有）
4. 本番での `SECRET_KEY` fail-closed 検証（`security_config.py`：未設定・デフォルト値・32文字未満は起動拒否）
5. `is_listed=False` 商品が商品一覧から除外され価格表にのみ出る挙動（`routes/main.py:205-337` の `Product.is_listed.isnot(False)`）
6. ソフトデリート（`deleted_at`）とアーカイブ（`archived`）のフィルタリング
7. 登録時の自動翻訳（`auto_apply`）とデフォルト価格ルール適用（PR #136 で導入。`tests/test_register_translate_pricing.py` が仕様）
8. スキーマ bootstrap の3層構造: Alembic（11 migration）→ `ADDITIVE_STARTUP_MIGRATIONS` legacy patchset → drift verify。web/worker どちらが先に起動しても schema が揃う性質（`SCHEMA_BOOTSTRAP_MODE=auto`）
9. scheduler lock（Redis or file lock）により patrol/trash purge の二重実行が起きないこと
10. inmemory queue 時に Gunicorn `--workers 1` 前提で動く single-web 互換経路
11. `cli.py` の全運用コマンドの出力契約（`tests/test_cli_*.py` 30ファイル超が出力構造を固定している）
12. パトロールの exponential backoff（`patrol_fail_count` / `next_patrol_at`）と SOLD 判定の hysteresis（`tests/test_mercari_sold_hysteresis.py`）
13. bg_removal の HMAC internal upload エンドポイントの CSRF exempt（`app.py:318-322`）

---

## 4. Non-Negotiables（作業上の絶対制約）

- 最初に `git status` を確認する。既存の未コミット変更と自分の変更を混ぜない
- 編集前に baseline の検証結果（`python -m pytest -q` の結果）を記録する
- 変更は小さく、1つずつ revert 可能な単位でコミットする
- 無関係な整形・ついでのリファクタリングをしない（diff は意図した変更のみ）
- 既存挙動を勝手に変えない。テストを実装に合わせて書き換えない
- `llama.cpp/` を編集しない
- DB schema（models / Alembic / ADDITIVE_STARTUP_MIGRATIONS）を本指示書で明示した範囲外で変更しない
- 公開ルート・認証・課金的計算（pricing）・外部連携（webhook / OpenAI / Render 契約）の挙動を変更しない
- 各フェーズ完了ごとに該当する検証コマンドを実行して green を確認する
- 正しさが不明な点に遭遇したら実装を止めて質問する

## 5. Stop And Ask Conditions（必ず停止して質問する条件）

- 削除候補ファイルがどこかから import されている・運用手順書（docs/）から参照されていることが判明した場合
- テストと実装が矛盾しているように見える場合
- 変更が Alembic migration の追加を必要とする場合
- 公開API（ルートURL・JSONレスポンス構造）、保存済みデータ、CSV 出力フォーマットに影響しうる場合
- `render.yaml` / 環境変数契約に触れる必要が出た場合
- セレクターヒーリング（`config/*.json(l)` の実行時書き込み）の挙動に影響しうる場合
- 複数の設計案がありプロダクト判断が必要な場合

---

## 6. Baseline Commands

各フェーズの前後で実行し、結果をログに残すこと。

```bash
git status && git log --oneline -3
python -m pytest -q                  # full suite（baseline: 698 passed, 1 skipped）
pytest tests/test_e2e_routes.py -q
pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q
python -m pip check
```

---

## 7. Debt Map（技術的負債一覧）

### D1. ルート直下の一回限りデバッグ／検証スクリプト群【死コード・配置の分かりにくさ】

- **根拠**: ルート直下に約25個の一回限りスクリプト: `test_active_item_live.py`, `test_active_mercari.py`, `test_fetch.py`, `test_mercari_full.py`, `test_mercari_html.py`, `test_mercari_speed.py`, `test_scraping_real.py`, `test_scrapling.py`, `test_search_html.py`, `test_single_csv.py`, `test_snkrdunk_search.py`, `debug_snkrdunk.py`, `debug_yahoo_repro.py`, `debug_yahoo_store.py`, `verify_fix.py`, `verify_isolation.py`, `verify_register.py`, `parse_dump.py`, `parse_mercari_html.py`, `analyze_dump.py`, `save_mercari_html.py`, `find_mercari_url.py`, `get_active_mercari_from_db.py`, `get_live_mercari.py`, `inspect_db.py`, `cleanup_db.py`, `create_test_user.py`, `add_patrol_fail_count.py`, `env_check.py`, `simple_test.py`, `run_tests_windows_hack.py`
- **なぜ負債か**: `pytest.ini` の `testpaths=tests` により CI から実行されず、ライブサイトに接続するものや古い前提のものが混在。新規参入エージェントが「どれが本物のテストか」を誤認するリスク
- **影響範囲**: なし（アプリ本体から import されていないことを Phase 2 で確認すること）
- **変更リスク**: 低（ただし運用で手動実行されている可能性 → 質問Q1参照）
- **改善案**: `scripts/dev/`（または `attic/`）へ移動。明らかに古いもの（`run_tests_windows_hack.py`, `simple_test.py`, `add_patrol_fail_count.py` — 同等の migration が Alembic 0008 に存在）は削除候補
- **検証**: `grep -rn "import <module名>"` で参照ゼロ確認 → full pytest green
- **実装可否**: **移動は実装可**。削除は質問Q1の回答後

### D2. ルート直下の Markdown レポート類の散乱【配置】

- **根拠**: `AUDIT_FINDINGS.md`, `reports.md`, `tasks.md`, `Request.md`, `tech_note_mercari_fix.md`, `deepbay_requirements.md`, `RESPONSIVE_DESIGN_ANALYSIS.md`, `SECURITY_ANALYSIS.md`, `SECURITY_CHECK_REPORT.md`, `UI_UX_AUDIT.md`, `Analysis_results/` 等
- **改善案**: `docs/work_reports/` が既にあるので、そこへ移動（AGENTS.md / README.md / SECURITY.md / DEVELOPMENT_STATUS.md / INCIDENT_RESPONSE.md / LICENSE_PENDING.md はルートに残す）
- **変更リスク**: 低。docs 内からの相対リンク切れだけ確認
- **実装可否**: **実装可**

### D3. サイト別スクレイパー7ファイルの重複と防御的 dead code【重複・責務】

- **根拠**:
  - `mercari_db.py:8-53`: `selector_config` / `selector_healer` / `scrape_metrics` の `try/except ImportError` + DummyMetrics フォールバック。これらのモジュールはリポジトリに常に存在するため到達不能な防御コードであり、本物の import エラーを握りつぶす
  - `yahoo_db.py`, `yahuoku_db.py`, `surugaya_db.py`, `offmall_db.py`, `snkrdunk_db.py` 等に `_empty_result()` / `_resolve_detail_url()` / metrics 呼び出しパターンがほぼ同型で重複
  - 戻り値 dict（url/title/price/status/description/image_urls/variants）の契約が暗黙（型・schema なし）
- **影響範囲**: 抽出とパトロール全経路
- **変更リスク**: 中（パトロールの status 判定はサイトごとに微妙に異なる。挙動を揃えようとしてはいけない）
- **改善案**:
  1. `try/except ImportError` フォールバックを通常の import に置換（mercari_db.py）
  2. `_empty_result` / `_resolve_detail_url` を `services/scrape_result_policy.py` あるいは新設 `services/scraper_common.py` に1実装へ集約し、各ファイルから import
  3. 戻り値契約を `TypedDict` または dataclass としてドキュメント化（変換はしない。型注釈のみ）
- **検証**: `pytest tests/test_mercari_item_parser.py tests/test_scraping_logic.py tests/test_snkrdunk_detail_parser.py -q` + full suite
- **実装可否**: 1・2 は**実装可**。3 は注釈追加まで実装可、実体の構造変更は提案に留める

### D4. セッション管理の二重性と private member 越境【境界の曖昧さ】

- **根拠**: `database.py` に `SessionLocal`（scoped_session）と `create_isolated_session()` が並存し、使い分け規約は docstring のみ。`routes/catalog.py:14` が private の `_session_factory` を直接 import している。`SessionLocal()` の直接利用が routes/services/jobs に約87箇所、`create_isolated_session` が16箇所
- **なぜ負債か**: scoped_session の `.close()` が他コードパスの session を巻き込む事故が既に発生している（PR #136 の fix コミット `116daa5` が証拠）
- **影響範囲**: 全ルート・全ジョブ
- **変更リスク**: 高（一括置換は危険）
- **改善案（最小）**: `routes/catalog.py` の `_session_factory` import を `create_isolated_session` に置換し、`database.py` に使い分けルールをモジュール docstring として明文化。**全面的な session 管理刷新は提案に留める**
- **検証**: `pytest tests/test_e2e_routes.py -q`（catalog ルートのテスト含む）
- **実装可否**: 上記最小変更のみ**実装可**

### D5. スキーマ管理の三重化【契約の曖昧さ】

- **根拠**: 新カラム追加時に (1) `models.py` (2) Alembic migration (3) `database.py: ADDITIVE_STARTUP_MIGRATIONS`（34エントリの生SQL）の3箇所更新が必要。直近の `is_listed` / `auto_apply` / `default_pricing_rule_id` もすべて3箇所に書かれている
- **なぜ負債か**: 更新漏れすると drift verify（`ensure_additive_schema_ready`）が起動失敗を引き起こす。仕組み自体は「web/worker どちらが先に起動しても良い」ための意図的設計
- **変更リスク**: 高（デプロイ契約そのもの）
- **改善案**: **コード変更はしない**。`database.py` のモジュール docstring か `docs/` に「カラム追加時の3点セット手順」を明文化するのみ。patchset の凍結（新規追加を Alembic のみへ一本化）はプロダクト判断 → 質問Q2
- **実装可否**: ドキュメント化のみ実装可

### D6. `cli.py` 3,624行の単一ファイル【責務の混在】

- **根拠**: 約30個の CLI コマンド（db smoke / stack smoke / render readiness / single-web 系 / selector repair / rich text maintenance 等）が1ファイルに同居
- **なぜ負債か**: 編集コンフリクトと認知負荷。ただし `tests/test_cli_*.py` 30ファイル超が出力契約を厳密に固定しており、テストの安全網は厚い
- **改善案**: `cli/` パッケージへ機能群ごとに分割（`cli/db.py`, `cli/render.py`, `cli/single_web.py`, `cli/smoke.py` 等）。`cli.py` は `register_cli_commands` を re-export して互換維持。**コマンド名・オプション・出力は一切変えない**
- **検証**: `pytest tests/test_cli_*.py -q`（全 CLI テスト）+ `flask --help` でコマンド一覧が変わらないこと
- **実装可否**: **実装可**（純粋な機械的移動に限る）

### D7. `app.py` の責務混在【責務】

- **根拠**: `app.py`（1,115行）に app factory に加えて scheduler の lock（file/Redis）・heartbeat・retry のロジック（`app.py:95-264` ほか後半）が同居
- **改善案**: scheduler lock/heartbeat/health snapshot を `services/scheduler_runtime.py` へ抽出。`app.py` からは import して呼ぶだけにする。public 名（`get_scheduler_health_snapshot` 等）は `app.py` から re-export して `worker.py` / テストの import を壊さない
- **検証**: `pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py tests/test_health_route.py -q` + full suite
- **実装可否**: **実装可**（機械的抽出に限る）

### D8. ログ・出力の不統一【ログ】

- **根拠**: `database.py:33` の `print(f"DEBUG: Using database URL: ...")`（import 時に毎回出力）。他は `logging` を使用
- **改善案**: `logging.getLogger("database").info(...)` へ置換（URL は既に redact 済みなのでそのまま）
- **実装可否**: **実装可**

### D9. SQLAlchemy 警告: users ↔ pricing_rules の循環FK【schema】

- **根拠**: テスト実行時の `SAWarning: unresolvable cycles between tables "pricing_rules, users"`（`users.default_pricing_rule_id` → pricing_rules、`pricing_rules.user_id` → users）。将来の SQLAlchemy でエラー化予告あり
- **改善案**: `use_alter=True` を FK に付与する案があるが、**既存DBへの影響と Alembic 整合が絡むため提案に留める** → 質問Q3
- **実装可否**: 実装不可（質問回答待ち）

### D10. `models.py` 内の未解決設計メモと README とのドキュメント乖離

- **根拠**: `models.py:27`（Shop.name の一意性についての長い迷いコメント）、`models.py:43`（source_url の unique 制約除去メモ）。README の products テーブル説明では `selling_price` が「Float」だが実際は `Integer`（`models.py:79`）
- **改善案**: README の型表記を実態（Integer）に修正。models.py のコメントは「現仕様: name はグローバル一意制約なし、source_url はユーザー間重複可」と事実だけ残して整理
- **実装可否**: **実装可**（コメント・ドキュメントのみ。制約の変更はしない）

### D11. lint / typecheck の不在【検証基盤】

- **根拠**: ruff / flake8 / mypy の設定ファイルなし。CI は pytest + pip-audit のみ
- **改善案**: `ruff` を最小ルール（F: pyflakes 系のみ、未使用 import・未定義名検出）で導入し CI に追加。フォーマッタ（ruff format / black）の一括適用は diff 汚染になるため**やらない**
- **検証**: `ruff check .`（`llama.cpp/` と `attic|scripts/dev` は exclude）が green、CI green
- **実装可否**: **実装可**（既存違反は exclude/noqa で抑制し、新規違反のみブロックする構成にする。既存コードの書き換えで対応しない）

### D12. 実行時に書き換わるファイルが git 管理下にある【設定/状態の混在】

- **根拠**: `config/heal_history.jsonl`, `config/element_fingerprints.json`, `config/scraping_selectors.json` はセルフヒーリングが実行時に更新する状態ファイルだが、リポジトリにコミットされている
- **変更リスク**: 中（初期値として読み込まれている。gitignore 化すると初回起動の挙動が変わる可能性）
- **改善案**: **提案に留める** → 質問Q4
- **実装可否**: 実装不可（質問回答待ち）

### D13. 命名の歴史的負債: `*_db.py` という誤解を招くスクレイパー名

- **根拠**: `mercari_db.py` 等はDBアクセスではなくスクレイパー。`sqlite:///mercari.db` というDB名も歴史的経緯
- **改善案**: `services/scrapers/` への移動 + リネームは import 箇所が多く（routes/jobs/services/patrol/tests）リスクの割に利益が薄い。**今回はやらない（Out-of-scope）**。DB名 `mercari.db` は保存済みデータに直結するため**絶対に変更しない**

---

## 8. Implementation Phases（小さく安全な順に実施）

> 各フェーズは独立したコミット（または PR）にし、フェーズごとに検証して green を確認してから次へ進む。

### Phase 0: 現状確認
1. `git status` がクリーンであることを確認
2. `python -m pytest -q` を実行し、結果（passed/skipped 数・所要時間）を記録 — baseline: 698 passed, 1 skipped
3. `python -m pip check` green を確認

### Phase 1: 安全網の確認（追加が必要なら先に作る)
1. これから触る範囲のテストカバレッジを確認する。D3（スクレイパー共通化）に着手する前に、各 `*_db.py` の `_empty_result` 相当の戻り値構造を固定する小さなテストが無ければ `tests/` に追加する（既存テスト `test_scraping_logic.py` 等で十分カバーされている場合は追加不要 — 判断根拠を報告に書く）

### Phase 2: 明らかに安全な整理（D1, D2, D8, D10）
1. ルート直下の各デバッグスクリプトについて `grep -rn "import <名前>" --include="*.py"`（`llama.cpp/` 除外）で参照ゼロを確認したうえで `scripts/dev/` へ `git mv`。**参照が見つかったものは動かさず Stop And Ask**
2. レポート系 Markdown を `docs/work_reports/` へ `git mv`（D2 の残置リスト厳守）
3. `database.py:33` の print → logging（D8）
4. README の `selling_price` 型表記修正 + `models.py` コメント整理（D10）
5. full pytest green 確認

### Phase 3: 小さな責務分離（D3-1, D3-2, D4 最小）
1. `mercari_db.py` の `try/except ImportError` フォールバック除去（通常 import 化）
2. `_empty_result` / `_resolve_detail_url` の共通化（1実装に集約、各サイトから import。**status マーカー等サイト固有ロジックは絶対に共通化しない**）
3. `routes/catalog.py` の `_session_factory` 直接 import を `create_isolated_session` に置換
4. `pytest tests/test_e2e_routes.py -q` + スクレイパー系テスト + full suite green

### Phase 4: 境界・インターフェースの明確化（D3-3, D5 ドキュメント, D7）
1. スクレイパー戻り値契約の TypedDict 化（注釈とドキュメントのみ。実行時動作不変）
2. `database.py` に「カラム追加3点セット手順」の docstring 追加
3. `app.py` から scheduler lock/heartbeat ロジックを `services/scheduler_runtime.py` へ機械的抽出（`app.py` から re-export して既存 import を維持）
4. worker/runtime テスト + full suite green

### Phase 5: テストしやすい構造（D6）
1. `cli.py` を `cli/` パッケージへ機械的分割（コマンド名・オプション・出力・`register_cli_commands` の公開シグネチャ完全維持）
2. `pytest tests/test_cli_*.py -q` + full suite green、`flask --help` のコマンド一覧 diff ゼロ確認

### Phase 6: 検証基盤（D11）
1. ruff を pyflakes 系最小ルールで導入（pyproject.toml または ruff.toml、`llama.cpp/` と `scripts/dev/` を exclude）
2. 既存違反は設定で抑制（per-file-ignores）。既存コードを書き換えて違反解消**しない**
3. CI（`.github/workflows/ci.yml`）に `ruff check .` ステップを追加
4. CI green 確認

### Phase 7: 提案のみ（実装禁止 — 承認が出るまで着手しない）
- D9（循環FK）、D12（config 状態ファイル）、D13（スクレイパー再配置）、session 管理の全面刷新、ADDITIVE_STARTUP_MIGRATIONS の凍結。それぞれ設計案と移行手順を Markdown でまとめて提出するのみ

---

## 9. Verification Requirements

- 各フェーズ後: そのフェーズの対象テスト + `python -m pytest -q`（full）を実行し green
- 最終: full suite が baseline と同等（698 passed, 1 skipped を下回らない。テスト追加分は増えてよい）、`python -m pip check` green、CI green
- 公開カタログに `source_url` / `site` が出ないことを `grep -n "source_url\|\bsite\b" templates/catalog.html routes/catalog.py` で目視再確認
- いかなるフェーズでも、テストの期待値を実装に合わせて書き換えることで green にしてはならない

## 10. Reporting Format

各フェーズ完了時に以下を報告する:

```
## Phase N: <名前>
- 変更ファイル一覧（git diff --stat）
- 実施した判断とその根拠（特に「移動せず残した」「共通化しなかった」判断）
- 実行した検証コマンドと結果（passed/failed 数をそのまま貼る）
- Stop And Ask に該当した事項（あれば）
```

最終報告には、最後に実行した全コマンドとその結果、および Phase 7 の提案ドキュメントへのリンクを含める。

## 11. Out-of-scope Items（今回やらないこと）

- `llama.cpp/` 配下の一切
- DB schema 変更（カラム・制約・index の追加/削除/変更）、Alembic migration の新規追加
- `*_db.py` スクレイパーのリネーム・`services/scrapers/` への移動（D13）
- session 管理の全面刷新（scoped_session 廃止等）
- フォーマッタ一括適用（black / ruff format で全ファイル整形）
- 機能追加（画像白抜き、価格表カテゴリ絞り込み、PayPal — AGENTS.md の未実装項目）
- `render.yaml` / デプロイ契約 / 環境変数の変更
- CSV エクスポートフォーマット・公開APIレスポンス構造の変更
- single-web 系 legacy コマンド・runbook の削除（互換用として意図的に残されている）

---

## 12. 実装前に確認すべき質問（人間の回答が必要）

**Q1.** ルート直下のデバッグスクリプト群（D1 のリスト）のうち、運用で今も手動実行しているものはあるか？ 無ければ `scripts/dev/` への移動で良いか、それとも完全削除して良いか。特に `create_test_user.py` / `cleanup_db.py` / `inspect_db.py` は運用ツールの可能性がある。

**Q2.** `ADDITIVE_STARTUP_MIGRATIONS`（database.py の生SQLパッチセット）は今後も新カラムごとに追記し続ける方針か？ それとも「全環境が Alembic 0010 以降に到達したら凍結し、以後は Alembic のみ」に切り替えてよいか（本番DBの現在のリビジョン次第）。

**Q3.** `users.default_pricing_rule_id` ↔ `pricing_rules.user_id` の循環FKに SQLAlchemy が警告を出している。`use_alter=True` を付ける修正（DBによっては DDL が変わる）を行ってよいか、現状維持か。

**Q4.** `config/heal_history.jsonl` / `element_fingerprints.json` / `scraping_selectors.json` は実行時に書き換わる。本番（Render）ではこれらの書き込みがどこに永続化されている想定か？ git 管理から外して初期値テンプレート＋実行時生成に分離してよいか。

**Q5.** Shop.name は現状グローバル一意制約なし（モデルコメントに迷いの跡）。「異なるユーザーが同名ショップを持てる」が正しい仕様という理解でよいか（指示書はこの前提で挙動維持としている）。
