# ESP 横断分析 — 2026-09-05

## 評価の要点

ESP は、国内商品の取り込みから編集・英訳・価格設定・バイヤー向けカタログ共有までを扱う業務アプリである。管理者と生徒アカウントの運用、非同期処理、画像背景除去まで実装が進んでおり、旧 README の「商品スクレイピングと CSV 出力」という説明だけでは範囲を表せない。

本調査時点で公開入口と依存サービスの health 応答は正常だった。対象コミットの CI は 1,668 件成功している。一方、顧客向けの外貨価格、巡回時の SKU 照合、公開商品と監視対象の関係に、業務上優先して整理すべき課題がある。全面的な作り直しより、価格・在庫・画像の整合性と分離構成の結合検証を先に固めるのが適切である。

この変更では README と本レポートを整備した。以下の実装上の指摘は未修正であり、テスト成功をもって解消したとは扱わない。

## 対象と確認範囲

| 項目 | 今回の確認 |
| --- | --- |
| リポジトリ | `halc8312/ESP`、公開リポジトリ |
| 基準コミット | [`2cc12a3e59f1d3e5374c727d580282ba6678afb1`](https://github.com/halc8312/ESP/commit/2cc12a3e59f1d3e5374c727d580282ba6678afb1)、取得時の `main` |
| 実装調査 | Flask の組み立て、ルート、モデル、サービス、ジョブ、テンプレート、JavaScript、マイグレーション、Docker / Render、CI、既存テスト |
| 規模の補助指標 | Git 管理下の Python 295 ファイル・84,527 行（テスト・補助スクリプトを含む）。`routes/` 内の route デコレータ84件。規模は品質評価点ではない |
| 公開サイト | `https://jp-items.com/`、`/readyz`、`/stack-readyz` に通常の読み取りリクエスト |
| GitHub Actions | 基準コミットの CI と同日の既存監視ジョブの結果・ログを確認 |
| Render 管理面 | 接続とワークスペース一覧まで確認。選択先未確定のため、サービス詳細・デプロイSHA・設定値・メトリクス・DB実体は未照合 |
| 実行していない範囲 | 認証後の本番操作、外部市場への取得、実データ更新、デプロイ、負荷試験、DB復元試験 |

README、`AGENTS.md`、過去の監査資料は設計意図の参考とし、機能の存在や挙動は実装と照合した。`llama.cpp/` は ESP 本体の調査・編集対象外とした。以下の行番号は基準コミットのもの。

## 実際の機能とデータの流れ

| 領域 | 実装から確認できること | 主な入口 |
| --- | --- | --- |
| 利用者管理 | セッション認証、管理者権限、生徒作成・停止・再開・パスワード再設定 | `routes/auth.py`、`routes/admin.py`、`services/student_account_service.py` |
| 商品取り込み | サイト別検索・単品URL、プレビュー、選択登録、手動登録、CSV | `routes/scrape.py`、`routes/import_routes.py`、`jobs/scrape_tasks.py` |
| 商品・販売価格 | 原価と販売価格、バリエーション、個別調整、ユーザー別価格ルール | `models.py`、`services/product_service.py`、`services/pricing_service.py` |
| 翻訳 | 提案保存、適用・却下、自動適用、手編集保護、期限切れ処理の回復、OpenAI / Argos | `routes/translation.py`、`jobs/translation_tasks.py`、`services/translator/` |
| 背景除去 | rembg、処理ジョブ、プレビュー、適用・却下、workerからwebへの結果転送 | `routes/bg_removal.py`、`jobs/bg_removal_tasks.py`、`static/js/product_bg_removal.js` |
| 公開価格表 | トークン公開、期限、停止、レイアウト、タグ・価格絞り込み、Quick View、通貨切替、アクセス集計 | `routes/pricelist.py`、`routes/catalog.py`、`templates/catalog.html` |
| 運用 | RQ、巡回、heartbeat、取得結果の観測と通知、DB移行用CLI | `worker.py`、`services/worker_runtime.py`、`services/monitor_service.py`、`services/scrape_health.py` |
| 外部販売への連携 | Shopify / eBay 向けCSV。直接出品・注文処理・決済・ストア在庫の自動同期は見当たらない | `routes/export.py` |

中心データは `User → Shop / Product / PriceList`。`Product` に `Variant` と `ProductSnapshot` が紐づき、`PriceListItem` が公開する商品と価格上書きを持つ。抽出ジョブ、翻訳提案、画像処理ジョブは別テーブルに記録する。仕入れ情報を持つ商品データと、顧客に返す公開用データを分けている点は維持すべき設計である。

標準の抽出画面は worker で取得してプレビューを返し、ユーザーの選択後に web 側で商品を保存する。翻訳や背景除去は別ジョブとして処理される。リストのみへの登録は通常一覧から分離されるだけでなく、定期巡回からも外れる。この違いは利用者向けにも説明が必要である。

取得対応は従来の7サイトに Record City と汎用商品URLの補助読み取りが加わっている。定期巡回は従来の7サイトに限られ、Record City・汎用URLには登録されていない。「取得対応」と「在庫監視対応」は同じ範囲ではない。

## 優先課題

優先度は改善の順序を示す。P1 は価格・在庫・保存の正しさに関わる項目、P2 は障害時や運用規模の増加で影響する項目。静的に確認した条件と、本番で発生した証拠を分けて記載する。

### P1-1 外貨価格の為替マージンがブラウザ取得成功時に消える

**確定した処理の不整合。** サーバーは利用者設定の為替マージンを反映した換算値を作るが、画面は外部API取得に成功すると加工前のレートへ置き換えて価格を再描画する。設定画面が約束する外貨価格の調整が、外部通信の成否によって変わる。

- 根拠: [`routes/catalog.py:48`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/routes/catalog.py#L48)、[`templates/catalog.html:469`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/templates/catalog.html#L469)、同ファイル485〜499行、`templates/settings.html:113,134`。
- 改善: 価格表示に使うレートとマージン適用をサーバー側に一本化するなど、成功・失敗の両経路で同じ規則を適用する。
- 受入条件: マージンあり／なし、外部取得成功／失敗、一覧／Quick Viewの組合せで価格が一貫すること。
- 本番で特定顧客に誤価格が提示された履歴までは確認していない。

### P1-2 巡回のバリエーション名照合が別SKUに一致し得る

**確定した照合条件の問題。** 巡回結果と既存バリエーションを名前の部分一致で照合し、最初の一致を更新する。包含関係のあるサイズ名などでは、別SKUの在庫・原価に更新が当たり得る。3つ目のオプションもこの照合には使われない。

- 根拠: [`services/monitor_service.py:358`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/services/monitor_service.py#L358) の更新ループ、特に372〜383行。`services/patrol/yahoo_patrol.py:80` は取得した選択肢名を渡す。
- 改善: 安定した外部ID、または正規化した全オプションの完全一致を使う。曖昧な対応は更新を保留する。
- 受入条件: 名前が包含関係にある複数SKU、複数オプション、選択肢の並べ替えでも対象SKUだけを更新すること。
- 本番データの誤更新や注文への影響は未確認。

### P1-3 公開リストに直接登録した商品は自動巡回されない

**意図された現行仕様であり、要件確認事項。** リスト専用登録は `is_listed=False`。巡回はこれを除外する一方、公開カタログは表示する。したがって「公開中だから仕入れ先の在庫・価格も自動追従する」とは限らない。

- 根拠: [`routes/scrape.py:647`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/routes/scrape.py#L647)、[`services/monitor_service.py:121`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/services/monitor_service.py#L121)、`routes/catalog.py:433`。既存テスト `tests/test_register_to_pricelist.py:66` と `tests/test_monitor_service.py:85` も個別にこの挙動を固定している。
- 改善: 依頼者と「公開商品の在庫鮮度」を決める。自動追従が必要なら一覧表示と監視対象のフラグを分ける。現仕様を保つ場合は登録時に対象外であることと確認責任を明示する。
- 今回: README に制約を追記。監視仕様は変更していない。

### P1-4 非プレビュー保存経路では画像の保存・配信先が分離する

**コードと Blueprint の組合せで確認した構成上の問題。現在の標準UI全体の障害ではない。** 非プレビューの `/scrape/run` は worker 内でDB保存と画像キャッシュを行う。通常の画像キャッシュはそのプロセスのローカルディスクへ保存して `/media/` URLを返すが、Blueprint の永続画像ディスクは web にしかない。

- 根拠: [`routes/scrape.py:192`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/routes/scrape.py#L192)、`jobs/scrape_tasks.py:128`、`services/product_service.py:400`、[`services/image_service.py:350`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/services/image_service.py#L350)、`render.yaml:18,54`、`app.py:407`。
- 範囲: 標準UIは `static/js/scrape_form.js:483` で `preview` を送信し、登録は web の `routes/scrape.py:549,647` で行う。この経路は上記の保存場所不一致の直接対象外。
- 追加事項: `cache_product_image` の保存先作成が例外保護の外にあり、書き込み不能なら外部URLへのフォールバックまで到達しない。Dashboardの設定・実際の書き込み可否・404発生件数は未確認。
- 改善: 旧保存経路の利用要否を明確にし、使うなら web への転送または共通の永続ストレージへ保存する。配信可能なことを確認してからURLを確定する。
- 受入条件: web / worker のファイルシステムを分けた状態で登録画像をwebから取得でき、再起動後も残ること。書き込み失敗時の状態を明示すること。

### P2-1 AUD / CAD の保存済みレートと画面選択肢が食い違う

画面は AUD / CAD を選択できるが、サーバーの日次保存対象には含まれない。保存済みレートが1件以上ある場合、完全なフォールバック表を使わず保存値だけを採用するため、欠けた通貨へ変更しても価格文字列が以前のまま残る。フィルターの通貨表示と数値の解釈にも不一致が生じる。

- 根拠: `services/exchange_rate_service.py:28`、`templates/catalog.html:36,324,513,551,579`。`tests/test_exchange_rates.py:88` は「画面がAUDを提供しない」という古い前提を持つ。
- 改善: 通貨定義を共有し、欠けた通貨を無効化するか、全対応通貨のレートを揃える。ブラウザ側の非同期動作も検証する。

### P2-2 再抽出と再計算にデータ整合性の未整理部分がある

初回登録は取得した複数バリエーションを作成するが、再抽出時はそのバリエーション一覧を再同期せず、単一／Default Titleの価格や商品状態に応じた数量だけを更新する。個別サイズの新しい価格・在庫・追加選択肢が再抽出だけでは反映されない。

また、商品・原価・履歴を先にコミットし、販売価格再計算を別のコミットで行う。再計算の例外は `False` に変換され、呼び出し元はその戻り値を確認しない。途中失敗時に新原価と旧販売価格が併存し得る。

- 根拠: `services/product_service.py:304,387,414`、[`services/pricing_service.py:294`](https://github.com/halc8312/ESP/blob/2cc12a3e59f1d3e5374c727d580282ba6678afb1/services/pricing_service.py#L294)。正常系の価格更新テストは存在する。
- 改善: 手編集値の保護と仕入れ情報同期の責任を分け、原価と再計算の確定を同一単位にするか、再計算待ち状態と再試行を管理する。

### P2-3 キューは分かれているが処理能力は共有している

`scrape` と `media` は1台の `SimpleWorker` が処理する。抽出・翻訳・背景除去の独立した実行能力があるわけではない。長時間ジョブや抽出の連続投入で、他機能の待ち時間が伸びる可能性がある。

- 根拠: `render.yaml:73`、`services/media_queue.py:66`、`services/worker_runtime.py:183,743`。抽出・mediaの既定タイムアウトはともに1800秒。
- 改善: まずジョブ種別ごとの待機時間・処理時間・最古待機ジョブを記録する。分離する場合はscheduler所有者、画像転送、heartbeatの定義も合わせて設計する。
- 現在のCPU不足、メモリ不足、キュー枯渇を観測したという意味ではない。

### P2-4 CIに本番と同じ分離構成の結合検証がない

通常CIは SQLite を使い、production smoke は設定値の検証である。Docker検証も重要なブラウザ起動や終了処理を確認するが、PostgreSQL・Redis・web・worker・画像配信を一式でつなぐ検証ではない。

- 根拠: `.github/workflows/ci.yml:10,38,41`、`tests/conftest.py:12`、`.github/workflows/docker-build.yml`。画像キャッシュテストは `tests/test_product_service.py:350` で保存処理をmockする。
- 改善: 外部市場に接続しない小さな結合テストで、PostgreSQL migration、Redis投入からDB反映、分離ディスク環境での画像配信を確認する。ブラウザの為替・Quick Viewも少数の画面テストで補う。

### P2-5 Quick Viewの失敗時に前の商品情報が残り得る

正常表示はタイトル・価格・在庫・画像を更新するが、次の商品取得に失敗した場合は説明文だけをエラーへ変更してモーダルを再表示する。直前に見た商品の商用情報が残る経路がある。

- 根拠: `templates/catalog.html:696,756,770`。
- 改善: 読み込み開始時に商品表示状態を初期化し、失敗表示を分ける。選択中の商品IDと応答を照合して古い応答を適用しない。
- 実ブラウザでの再現操作は今回行っていない。

### P2-6 アクセス解析が全履歴を毎回読み込む

価格表の全アクセス履歴をORMオブジェクトとして取得し、累計・期間別の集計をPythonで計算する。表示に必要なのは集計値、14日分のグラフ、直近20件であり、長期運用で読み込み量が増える。

- 根拠: `routes/catalog.py:533,545,555,590`。リスト削除時以外のアクセス履歴の定期削除は今回のコード検索では見当たらなかった。
- 改善: SQL集計、日付条件、直近一覧の `LIMIT` を使う。保存期間を決める。
- 現在の実件数・遅延は未測定。

### P2-7 移行警告・依存監査の例外を追跡する必要がある

基準CIには、`users.default_pricing_rule_id` と `pricing_rules.user_id` の循環参照を `Base.metadata.sorted_tables` が並べ替えられない SQLAlchemy 警告が3件ある。また依存監査では `CVE-2026-54499` が除外されている。

- 根拠: `models.py:14,220`、`services/database_migration.py:29`、`.github/workflows/ci.yml:31` と基準CIログ。
- 改善: 移行順序と循環参照の扱いを明示し、本番同等のPostgreSQLで検証する。依存の例外には担当者・解除条件・見直し時点を置く。
- 監査成功は、除外された問題の解消を示さない。本番でその問題が悪用可能かの検証は今回の対象外。

### P2-8 運用資料に相反する構成の説明が残る

`render.yaml` と `AGENTS.md` は split を現在の契約とする一方、`docs/RENDER_CUTOVER_RUNBOOK.md:3,5,9,76` は single-web が現行、Blueprint は dormant と説明する。同じrunbook内に新しいsplitの詳細も混在する。旧READMEとAGENTSの「背景除去未実装」も現在のAPI・ジョブ・画面に一致しない。

- 改善: READMEを現在の実装の入口にし、現行運用手順と過去の切替記録を分ける。READMEの修正だけでは既存runbook本体の矛盾は解消されない。
- 今回: READMEを一新し、矛盾箇所と利用範囲を明示した。運用構成や承認済み設定の変更はしていない。

## 維持すべき良い実装

- **公開範囲の明示:** カタログ一覧・詳細は価格表所有者で商品を絞り、公開用の辞書を作る。仕入れ情報を含むORM全体を返さない。画像は管理されたローカルURLに限定する。`routes/catalog.py:101,176,323,433,489`。
- **アカウント停止との連動:** 停止した所有者の価格表を非公開扱いにし、既存セッションも次のリクエストで拒否する。`routes/catalog.py:176`、`app.py:283`。
- **防御用の基本処理:** rich textの許可リスト、外部画像の検証・サイズ制限、所有権確認、CSRF、本番用cookie設定があり、境界の既存テストもある。存在確認であって網羅的な安全性保証ではない。
- **取得失敗と売切れを分ける:** 不確実な結果を即座に売切れへ変換せず、状態証拠や信頼度を参照する。正常な検索0件と取得障害も区別している。`services/monitor_service.py:176`、`jobs/scrape_tasks.py:36`。
- **分離構成向けの仕組み:** PostgreSQL起動時migrationのadvisory lock、worker終了時のscheduler・ブラウザ停止、背景除去のwebへの結果受け渡しがある。`database.py:310`、`services/worker_runtime.py:639`、`jobs/bg_removal_tasks.py:237`。
- **監視の意味を区別:** `/readyz` と `/stack-readyz`、オフラインfixtureと実稼働heartbeatを分けている。テスト結果を市場の稼働証明と取り違えない設計は継続する。

## 本番確認と検証結果

| 確認 | 結果と限界 |
| --- | --- |
| 基準コミットの[CI](https://github.com/halc8312/ESP/actions/runs/33934970540) | Ubuntu / Python 3.11。`1668 passed, 1 skipped, 3 warnings in 179.71s`。依存整合性・監査・本番設定smokeも成功 |
| 依存監査 | `No known vulnerabilities found, 1 ignored`。除外内容は上記参照 |
| ローカル構文検査 | Git管理下のPython 295ファイルを `ast.parse` で解析し、構文エラー0件。import・実行の成功を意味しない |
| ローカルpytest | 実行0件。隔離環境への依存導入がpipのSHA-256不一致検証で停止し、その後の試行もpytest未導入で起動できなかった。整合性検証は回避せず、既存CIの結果を別の証拠として採用 |
| 公開入口、2026-09-05 16:45頃 UTC | `/` はHTTP 302でログインへ移動 |
| `/readyz`、同日16:46〜16:47 UTC | HTTP 200、`database=ok`、`redis=ok`、`rq`、`runtime_role=web`、web schedulerは無効 |
| `/stack-readyz`、同時刻 | HTTP 200、database / redis / worker / scheduler / patrol / scrape_monitor の6項目がすべて `ok` |

既存の[10:23 UTC監視](https://github.com/halc8312/ESP/actions/runs/33960543988)は `patrol=stale` で失敗した。他の5項目は正常だった。その後の[16:25 UTC監視](https://github.com/halc8312/ESP/actions/runs/33977805638)は6項目正常で、無効化によるスキップではなく実際のチェックが成功していた。今回の直接確認も正常である。ただし、一時的なstaleの原因はRenderログ・処理時間と未照合であり、原因解消を証明したものではない。

監視workflowの既定宛先はRender標準ドメインで、リポジトリ変数による上書きが可能。現在その変数が設定されているかは未確認である。独自ドメイン `jp-items.com` のDNS・TLS・入口の障害まで定期監視対象に含むかは、運用確認時に照合する必要がある。毎時監視は即時検知のSLAではない。

## 依頼開発としての次の進め方

1. **受入条件を揃える:** 外貨表示の算出規則、公開商品の在庫鮮度、リスト専用品とRecord Cityの扱い、CSV出力後の責任範囲を依頼者と合意する。
2. **価格と在庫の確定不整合を修正する:** 為替マージン、通貨選択肢、SKU照合、再計算の確定単位を小さな変更で直す。
3. **本番と同じ境界を検証する:** PostgreSQL / Redis / 別プロセス・別ファイルシステムの結合確認をCIに追加し、旧保存経路の利用要否を決める。
4. **運用引継ぎを完成する:** web / workerのデプロイSHA、実プラン・リージョン、共通設定、ジョブ滞留、巡回staleのログ、DBと画像の復元手順、アラートの受信実績を照合する。
5. **資料の保守範囲を絞る:** `cli.py` は3,912行、`app.py` は1,514行あり、旧互換手順も多い。機能変更時に責任単位で分け、同じ事実を複数資料へ重複記載しないようにする。

リポジトリが公開状態であること、`LICENSE_PENDING.md` が未決定であること、利用規約・プライバシーポリシーがドラフトであることは確認できる。受託成果物の公開範囲、権利、費用負担、運用責任が合意済みかはコードだけでは判断できないため、引継ぎ資料に確認済み事項として残す必要がある。

残る管理面の照合にはRenderの対象ワークスペースの確定が必要である。それが済むまでは、Blueprintと実環境の完全一致、デプロイSHA、バックアップの有効性、負荷の余裕を確認済みとはしない。
