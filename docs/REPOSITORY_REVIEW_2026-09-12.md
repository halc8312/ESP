# ESP リポジトリ横断レビュー — 2026-09-12

## 1. 対象と結論

調査対象は`halc8312/ESP`の`main`、commit **`72a8e6df47ebd9ec2bf4d4047b4511f325ec4317`**です。README、ルート・モデル、抽出／監視、worker・キュー、翻訳・画像・メール、設定、Docker、テスト・CIを横断して照合しました。全ファイル・全実行分岐の網羅監査ではありません。

現行実装は「単一プロセスの7サイトスクレイパー」ではなく、商品管理・公開カタログと、RQ workerでの抽出・翻訳・画像処理を組み合わせた構成です。古いREADMEには、後から追加された機能やsplit構成と矛盾する説明が残っていました。

**この変更で修正したのは文書です。下記の実装上の問題を修正したものではありません。** `render.yaml`、アプリコード、依存関係、DB、実デプロイ、外部メール送信は変更していません。

重要な発見は、パトロール結果の反映で、別サイズの在庫を変更し得ることと、売切れ・削除状態なのに在庫が正数へ戻り得ることです。該当メソッドを切り出した実行で確認しました。一方、画像処理の中断回復、PostgreSQL移行、キュー待ち時間などは、静的に見つかったリスクと追加検証事項を分けています。

### 証拠の区分

| 区分 | 意味 |
|---|---|
| コード確認 | 対象commitの実装・設定から読み取れる事実 |
| 切り出し再現 | DB等をstubにした対象メソッドの局所実行。アプリ全体のテストではない |
| 既存CI確認 | 対象commitについて既に実行されたGitHub Actionsの結果・ログを読んだもの |
| 要追加検証 | 故障・競合・負荷条件の仮説。本番で発生したという主張ではない |

## 2. READMEの修正内容

| 旧記述・不足 | 修正した説明 | 主な根拠 |
|---|---|---|
| 7サイト対応のみ | 抽出はRecord Cityを含む8サイト、パトロールは7サイト | `services/scrape_request.py`, `jobs/scrape_tasks.py`, `services/monitor_service.py` |
| アーキテクチャがinmemoryだけ | 本番RQ / PostgreSQL / Redis / dedicated workerと、inmemory互換経路を分離 | `render.yaml`, `worker.py`, `services/worker_runtime.py` |
| HTTP / browserの並列枠を全キュー共通として説明 | inmemoryの実行枠とRQ SimpleWorkerを区別 | `services/scrape_queue.py`, `services/worker_runtime.py` |
| ジョブのDB永続化が未実装 | `ScrapeJob` / event等は実装済み。ただし永続記録と自動回復は別 | `models.py`, `services/scrape_job_store.py` |
| 画像白抜きが未実装 | 投入・処理・結果返送・適用／却下は実装済み。モデルや返送設定の確認が必要 | `routes/bg_removal.py`, `jobs/bg_removal_tasks.py`, `services/bg_remover/` |
| Chromeの導入処理を削除済み | Dockerはbranded Chromeを導入し、Record Cityが利用 | `Dockerfile`, `render.yaml` |
| 15分おきに全登録商品を巡回 | 対象条件・再試行時刻を満たす商品を通常50件ずつ処理 | `services/monitor_service.py`, `render.yaml` |
| 指数バックオフ | `min(15分 × fail_count, 180分)`の線形バックオフ | `services/monitor_service.py` |
| パトロールが自動アーカイブ・snapshot保存 | 現行反映処理自体にはその処理がない | `MonitorService._apply_patrol_result` |
| `create-user`で管理者作成 | 既定はstudent。管理権限は`set-user-role`で別途付与 | `routes/auth.py`, `models.py` |
| ローカル手順でBash / PowerShell / Windowsランチャーが混在 | Bash例を統一し、PowerShellの読替えを明記 | 起動entrypoint、既存CLI、`docker-compose.local.yml` |
| Docker例が相対SQLite・書込み権限・永続化を考慮しない | 開発用volume、書込み先、非root実行の前提を明記 | `Dockerfile`, `database.py` |
| MySQLをPostgreSQLと同列の推奨DBとして掲載 | 同梱driver・構成に合わせSQLite / PostgreSQLを案内 | `requirements.txt`, `database.py`, CI |
| 公開カタログの絞り込み・期限・受付、ロールの説明不足 | タグ絞り込み等を追記。受付と予約・決済を区別 | `routes/catalog.py`, `templates/catalog.html`, `models.py` |
| Resendの設定・CLIの説明なし | 無効既定、設定確認、dry-run、明示的実送信を追加 | `services/mail_service.py`, `services/mail_cli.py`, `.env.example` |
| CI成功・preload成功の意味が曖昧 | 単体テスト、統合テスト、実推論、実到達性を分離 | `.github/workflows/`, `tests/conftest.py`, preload実装 |

重複していた運用CLIの説明は用途別にまとめ、詳細runbookへの導線を維持しました。古い固定モジュール数・モデル数は責務別の一覧へ置き換えています。Blueprintの前提を、Dashboardの実設定を今回確認したという表現にはしていません。

## 3. 現状の評価

### 維持したい設計

公開カタログでは所有者との対応、停止アカウント、公開状態・期限を確認し、外部仕入れ画像URLや内部情報をそのまま露出させない処理があります。商品価格も取得元価格と販売価格を分離し、明示的な0円を含む価格解決を共通化しています。これらの境界はリファクタリング時に維持すべきです。

本番設定は短い／未設定secretや共有レート制限ストア不足を拒否する設計で、web readinessとstack readinessも分けられています。翻訳には原文ハッシュ・手動編集の所有権とleaseがあり、単にバックグラウンド実行するだけではない保護があります。メール基盤も無効既定、dry-run、冪等性キーを持っています。

テスト側でセレクター自己修復の書込み先を一時領域へ隔離する点、Docker workflowで実際のChrome / Xvfbと終了処理を確認する点も有用です。ただし、これらは全ルートの安全性認証や本番可用性の保証ではありません。

根拠: [公開カタログ](../routes/catalog.py)、[価格解決](../services/pricing_service.py)、[本番設定](../security_config.py)、[モデル](../models.py)、[メール](../services/mail_service.py)、[テストfixture](../tests/conftest.py)、[Docker検証](../.github/workflows/docker-build.yml)。

### 構造的な改善方向

新機能を増やすより先に、商品状態・バリエーション在庫、ジョブ状態遷移、永続データの整合性を固める効果が大きいと評価します。webとworkerの分離を巻き戻す必要はありません。既存のモジュール境界を維持しながら、状態更新の条件を厳密にし、本番に近い統合テストを追加する方針が適しています。

## 4. 優先度付きの指摘

P1は優先して修正または影響評価する項目、P2は安定運用・再現性の改善、P3は保守性・文書管理です。同じ優先度でも、再現済み不具合と検証待ちのリスクを同一視しないでください。

### F-01 / P1: サイズ名の部分一致で別バリエーションを更新する

**区分: コード確認 + 切り出し再現。**

[MonitorService._apply_patrol_result](https://github.com/halc8312/ESP/blob/72a8e6df47ebd9ec2bf4d4047b4511f325ec4317/services/monitor_service.py)は、取得した`name`と既存`option1_value`を、`var_name in existing_name or existing_name in var_name`で照合し、最初の一致で打ち切ります。

既存順がXL、Lで、取得結果がLの場合、LがXLに含まれるためXLの在庫を変更します。空文字も任意の文字列に含まれるため、取得名が欠落している場合は先頭バリエーションを変更できます。`option2_value`や`option3_value`を含む複数軸の識別にもなっていません。

**影響:** 別サイズ・別選択肢の在庫、場合によっては取得元価格を誤更新する可能性があります。販売・公開表示への実際の影響範囲は統合テストで確認が必要です。

**改善案:** 取得元の安定したvariant ID / SKUが使える場合はそれを優先し、なければ正規化したオプションtupleの完全一致を使います。空名・複数候補・未対応形式は更新せず、観測記録へ残します。旧データとの対応表を用意し、部分一致を単に別の曖昧一致へ置き換えないことが重要です。

**受入条件:** LとXL、SとXS、空名、None、複数オプション、重複名、候補順序の入替えについて、対象以外の在庫・販売価格が変わらないこと。元メソッドを実ORMで呼ぶ回帰テストを追加します。

### F-02 / P1: 売切れ・削除判定後に正数在庫へ戻り得る

**区分: コード確認 + 切り出し再現。**

同じメソッドは`status in (sold, deleted)`で全在庫を0にしますが、その後に`result.variants`の反映ループも実行します。矛盾する取得結果に正数のstockがあると、商品状態はsold / deletedのまま在庫だけが正数になります。

**確認した条件:** 既存在庫7、結果status=sold、variants=[L:2]で、最終状態はsoldかつL在庫2です。deletedでも同じ結果です。矛盾するpayloadが本番で発生したことまでは確認していません。

**改善案:** 状態と在庫の優先関係をデータ契約として定義します。終端状態の結果を適用する場合、その反映処理内ではvariant payloadで在庫を戻さないようにします。将来の別巡回で再出品を検出した場合の復帰は別の遷移です。価格・snapshot・ユーザー入力との関係も明示します。

**受入条件:** sold / deletedでは全variant在庫0が保たれること。矛盾payloadは記録されること。公開カタログ、価格表、CSVの在庫・売切れ表示まで同じ条件で検証すること。

#### 切り出し再現の結果

対象メソッドを照合元commitから転記し、DBのquery・Variant構築と状態正規化だけをstubにしました。入力statusは既に正規形です。実行環境はPython 3.13.5で、ESPのPython 3.11環境・pytest全体・実サイトスクレイピングを実行したものではありません。

| 入力・条件 | 観測した最終在庫 | 評価 |
|---|---|---|
| Lだけが存在、Lのstock=2 | L=2 | 対照ケース |
| XL=7、L=7の順で存在、Lのstock=2 | XL=2、L=7 | 誤ったサイズを更新 |
| XL=7、L=7の順で存在、name空、stock=0 | XL=0、L=7 | 空名で先頭を更新 |
| status=sold、L=7、取得Lのstock=2 | 状態sold、L=2 | 終端状態と在庫が矛盾 |
| status=deleted、L=7、取得Lのstock=2 | 状態deleted、L=2 | 終端状態と在庫が矛盾 |

この5ケースは「既存pytestの新しい5件が成功した」という結果ではありません。修正時は、転記した実装ではなくリポジトリの実メソッドを呼ぶ回帰テストへ移してください。

### F-03 / P2: 画像ジョブの中断回復と競合制御を補強する

**区分: コード確認。故障・競合の統合再現は未実施。**

[画像job store](https://github.com/halc8312/ESP/blob/72a8e6df47ebd9ec2bf4d4047b4511f325ec4317/services/bg_remover/job_store.py)では、ORMで状態を読み、Python側で判定して更新・commitします。[ImageProcessingJob](../models.py)には翻訳提案のようなworker token / leaseがなく、[worker起動時](../services/worker_runtime.py)で確認できる回復対象は抽出と期限切れ翻訳です。[画像タスク](../jobs/bg_removal_tasks.py)は通常の例外をfailedへ反映しますが、プロセスの強制終了ではそのexceptが走らない場合があります。

**想定される問題:** runningのまま残ったジョブ、重複実行、古いworker結果が利用者の却下などと競合する状況です。既存のterminal状態チェックはありますが、読取りと更新の間の競合をDB条件で防ぐ方式とは異なります。

**改善案:** queued→runningのclaimを条件付き更新にし、lease / attempt / worker tokenを付与します。終端状態への遷移も期待状態・tokenを条件にし、アップロード／適用の冪等性を確保します。期限切れの処理は起動時だけでなく定期的に整理し、ファイルの孤立も回収できるようにします。

**受入条件:** 同じjobを2workerで処理、推論中SIGKILL、アップロード直後の停止、却下と完了の競合、再試行後の古い結果返送を実Redis / PostgreSQLで検証し、状態と画像が一意に確定すること。

### F-04 / P1（影響評価）: Stanzaの監査除外を期限付きで管理する

**区分: 依存・CIログ確認 + upstream advisory確認。ESPへの攻撃・悪用は確認していません。**

[CI](../.github/workflows/ci.yml)は`CVE-2026-54499`を監査から除外しています。対象CIではArgos 1.11.0からStanza 1.10.1が導入され、監査結果は`No known vulnerabilities found, 2 ignored`でした。除外後の成功を「既知脆弱性ゼロ」とは扱えません。

[Stanza公式advisory](https://github.com/stanfordnlp/stanza/security/advisories/GHSA-v5jw-96jm-7h2c)では、影響範囲は1.12.1以下、修正版は1.12.2以上です。信頼できないモデルファイルを読み込む際の危険なデシリアライズが問題であり、攻撃者が読み込まれるモデルを配置・汚染できる等の条件が関係します。公開Webページを開くだけでESPが直ちに侵害される、という証拠ではありません。

**改善案:** モデルの入手先・固定hash・更新手順・cache書込み権限と、実際に通るloader経路を確認します。例外には責任者、理由、代替策、見直し期限、追跡項目を持たせます。ArgosがStanzaを固定しているため、Stanzaだけを競合する版へ上書きするのではなく、互換な依存解決・モデル・翻訳回帰テストをセットで確認します。

[translator registry](../services/translator/registry.py)にはOpenAIからArgosへのフォールバックもあります。`TRANSLATOR_BACKEND=openai`であることだけを理由にArgosを未使用と判断したり、依存を削除したりしないでください。フォールバックを外す場合も、可用性の仕様変更として扱います。

**受入条件:** 除外なし監査の結果を保存し、対象loaderの到達条件とモデルの信頼境界を説明できること。互換版へ更新できた場合は例外を削除し、通常翻訳・フォールバック・起動のテストが成功すること。

### F-05 / P2: 背景除去の利用可能性をビルド成功だけで判定しない

**区分: コード・依存・CI確認。画像推論は今回未実行。**

[preload.py](../services/bg_remover/preload.py)はimportやモデル事前読込に失敗しても警告を出して終了コード0で戻ります。[Docker検証](../.github/workflows/docker-build.yml)にも画像白抜きの実推論は含まれません。そのため、イメージのビルドが成功していても画像機能の利用可能性は未確定です。

また、[rembg 2.0.75の依存定義](https://github.com/danielgatis/rembg/blob/v2.0.75/pyproject.toml)ではONNX Runtimeはoptionalで、CPU extraに含まれます。ただし、**今回のCIでは`argostranslate → minisbd → onnxruntime`からONNX Runtime 1.30.0が実際に導入されています**。従って、`rembg`に`[cpu]`がないことだけから「現在のESPでは背景除去が動かない」とは結論しません。

**改善案:** 画像機能を提供する構成ではCPU依存を明示し、別機能の推移依存に依存しないようにします。開発時のoffline許容と本番imageの必須モデル検証を分離します。モデルを準備したDockerで小画像を実処理し、PNG形式・alpha・出力寸法・画像返送を確認するsmokeを追加します。

**受入条件:** ネットワークやモデルが欠けた場合に、意図した構成ではbuild / readiness / capability診断のいずれかが明確に失敗すること。モデルを用意した構成では実推論が成功すること。翻訳依存を外した構成でも画像依存が不足しないこと。

### F-06 / P2: 推移依存とCPU実行向けパッケージを制御する

**区分: requirementsと既存CIログ確認。サイズ・速度改善の実測は未実施。**

直接依存は固定されていますが、推移依存は今回のCIでも新しい版へ解決されています。Argos / StanzaからTorch・CUDA系の大きな依存が導入され、ログにはTorchの554.6 MB、cuDNNの553.1 MBのwheel取得が記録されています。これらはwheelの転送サイズであり、最終Docker imageサイズではありません。

**改善案:** lock / constraintsとhashを含む再現可能な解決結果を用意し、CPU構成で必要なwheelの選択を明示します。web・取得・翻訳・画像の依存を分割する場合は、ArgosフォールバックとONNX Runtimeの隠れた依存関係を先に整理します。build時間、imageサイズ、起動時間、ピークRSSを変更前後で比較してください。

**受入条件:** 同じlockで解決結果が再現でき、`pip check`、監査、翻訳フォールバック、画像推論、ブラウザsmokeを通ること。削減率や必要メモリは測定するまで保証しないこと。

### F-07 / P2: PostgreSQL / Redisの統合テストを通常pytestと分離して追加する

**区分: workflow・fixture確認。**

[ci.yml](../.github/workflows/ci.yml)はSQLiteを指定し、[conftest.py](../tests/conftest.py)もimport時とfixture内でSQLite DBを設定します。単にCIにPostgreSQLの環境変数を渡すだけでは、実PostgreSQL向けの統合テストへ切り替わりません。Docker側の実ブラウザ検証は存在するので、「実環境を使うテストが全くない」という評価も誤りです。

**改善案:** 実PostgreSQL・Redisを使う別fixture / workflow / markerを設け、既存のSQLite fixtureによる上書きから分離します。RQ投入→worker→DB保存→status API、worker終了・再起動、同時schema bootstrap、所有者分離、画像の内部返送を検証します。外部サイトを使わないfixture payloadから始めると、サイト側の揺らぎとアプリの不具合を分けられます。

**受入条件:** 実際のDB dialectとRedis接続先をテスト開始時にassertすること。破棄可能なデータ領域を使い、live DBやメール・課金APIに触れないこと。

### F-08 / P2: DB移行の循環外部キー警告を解消する

**区分: 既存CI警告 + コード確認。実PostgreSQL移行失敗は未確認。**

既存CIでは`tests/test_database_migration.py`の3ケースから、`pricing_rules`と`users`の循環参照を理由にSQLAlchemyがtable順を正しく解決できない警告が出ています。発生箇所は[database_migration.py](../services/database_migration.py)の`_repo_table_order()`にある`Base.metadata.sorted_tables`です。

**改善案:** 相互参照データを含む移行fixtureを作り、ユーザー／価格ルールの二段階投入、遅延可能な制約、復元後の参照更新など、実際のschemaに合う方法を選びます。警告を非表示にするだけではなく、移行・restoreの順序と検証条件を明示してください。

**受入条件:** デフォルト価格ルール付きユーザーを含むSQLite→PostgreSQL移行、件数だけでなく参照整合性、sequence、途中失敗の扱いを確認すること。警告の消失とデータ正当性を両方検証します。

### F-09 / P2: キューごとの待ち時間と処理資源を見える化する

**区分: worker構成確認。飢餓・性能劣化は負荷再現していません。**

[worker_runtime.py](../services/worker_runtime.py)はscrape / mediaのqueueを1つの`SimpleWorker`へ渡しています。queue名を分離するだけでは処理プロセスやCPU・メモリは分離されません。抽出・翻訳・画像の処理時間が違うため、総ジョブ数だけでは利用者が待たされている機能を把握しにくくなります。

**改善案:** queue別の最古待機時間、実行時間分布、失敗率、処理済み件数、ピークRSSを計測します。既存の起動時backlog診断とheartbeatに加え、継続的なqueue進捗の観測を検討します。処理順序の変更や専用media workerは、負荷テストとリソース・運用契約の確認後に選択します。

**受入条件:** 長い抽出を連続投入した状態でも、翻訳・画像の待ち時間を測定でき、合意した上限を満たすか判断できること。このレビューではサービスを増設していません。

### F-10 / P2: Docker初期起動の書込み先をsmokeで確認する

**区分: Dockerfile・DB既定値の静的確認。修正READMEのDocker手順は今回実行していません。**

[Dockerfile](../Dockerfile)は`/app`を作業ディレクトリにし、非rootの`myuser`で起動します。DBの既定値は相対パスのSQLiteです。旧READMEは明示的な書込み先・volume・権限準備を省略しており、そのまま使える前提が不足していました。本番BlueprintはPostgreSQLと画像用の永続領域を別途指定しています。

**対応した文書修正:** ローカル例を新規開発volumeの初期化と絶対SQLiteパスへ変更し、画像・インポートプレビューの保存先も明示しました。rootを使うのは新規volumeの初期化だけで、アプリの常時root実行は勧めていません。

**追加の受入条件:** 非rootで空volumeから起動、ユーザー作成、画像アップロード、CSV preview、コンテナ再作成後のデータ保持をDocker CIで検証します。

### F-11 / P3: サイト能力・環境変数・文書の単一管理を進める

**区分: コード・文書の照合。**

抽出リクエスト、タスクdispatch、監視の対象、既存CLI・live受入の対象、READMEにサイトの一覧が分散しています。Record City抽出があることと、パトロール／全CLIで同じ能力を持つことは別です。また`.env.example`にはメールがある一方、画像・翻訳などの設定案内は一箇所に集まっていません。AGENTS.mdにも画像白抜きを未実装とする古い記述があります。

**改善案:** サイトごとにsearch / detail / patrol / fixture / acceptanceなどの能力を宣言し、dispatchと文書表の整合性をテストします。環境変数は型、一般既定値、entrypoint上書き、secret、適用プロセスを区別して一覧化します。AGENTS / runbookの記述も責任範囲を維持して同期してください。今回のREADME更新だけで全関連文書を修正したわけではありません。

**受入条件:** サイト追加時に必要な能力宣言・テストが欠ければCIで検知できること。README相対リンク・anchor・コマンドの`--help`確認を文書CIへ追加すること。

### F-12 / P3: 大きいentrypointを責務単位で段階的に分割する

**区分: ファイル構造からの保守性評価。**

`app.py`と`cli.py`に、アプリ構成、診断、local rehearsal、legacy運用など多くの責務が集まっています。`services/`への分離は進んでいるため、全面的な書き直しより既存の境界を使う方が安全です。

**改善案:** health / readiness、scheduler lifecycle、Blueprint診断、DB移行、メール等をsmall service / CLI groupへ移し、従来のコマンド名・JSON出力・終了コードをcontract testで固定します。legacyコマンドの削除は利用状況を確認してから行います。

**受入条件:** import副作用、CLIの既存呼出し、web / worker role、各smokeの戻り値が変わらないこと。機能変更と大規模移動を同じPRに混ぜないこと。

## 5. 検証結果と限界

| 項目 | 今回確認した内容 |
|---|---|
| レビュー対象 | main `72a8e6d…`。README更新前にもmainの同一SHAを再確認 |
| 既存pytest | [CI run 34678204676](https://github.com/halc8312/ESP/actions/runs/34678204676)、[job 103511776910](https://github.com/halc8312/ESP/actions/runs/34678204676/job/103511776910)のログで`1804 passed, 1 skipped, 3 warnings in 204.61s`を確認 |
| 依存整合性 | 同じCIログで`No broken requirements found` |
| 脆弱性監査 | 同じログで`No known vulnerabilities found, 2 ignored`。CVE除外あり |
| 既存Docker CI | [run 34678204643](https://github.com/halc8312/ESP/actions/runs/34678204643)はsuccess。workflowはChrome / XvfbとSIGTERM処理を検証 |
| 局所再現 | パトロール反映メソッドの5ケースをstub環境で実行。対照1件と問題ケース4件 |
| 今回実行していないもの | ESPのpytest全体、実PostgreSQL / Redis統合、Docker build / 起動、画像実推論、実サイト受入、Renderの実設定・本番DB確認、外部メール送信 |

ローカルからのリポジトリ取得はネットワーク制約で成立せず、ソースはGitHub接続経由で読みました。従って、この作業でESPの依存一式を導入して全テストを再実行したという結果ではありません。上記の既存CI結果はレビュー元commitの証拠であり、この文書PRの新しいCI結果とは区別してください。

修正READMEのコマンドは実装・設定と照合して整備しましたが、このセッションで一連のセットアップを実行してはいません。特にDockerの権限・永続化やモデル利用可能性は、F-05 / F-10の実行確認が必要です。

## 6. 改善の進め方

まずF-01 / F-02を一つの小さな在庫整合性PRにし、対象メソッドの回帰テストと公開表示／CSVへの影響確認を追加することを推奨します。同時にF-04のモデル経路・監査例外の影響評価を行います。

次に、実PostgreSQL / Redisのテスト基盤を用意し、画像ジョブのlease・条件付き更新と移行の相互参照テストを進めます。その後に依存lock・CPU構成・モデル実推論smokeを整備します。最後に、queue別メトリクスを基に並列化を判断し、サイト能力・文書・大きなCLIの整理を段階的に進める順序が適切です。

本レビューに含むのは調査・文書修正であり、これらの改善PR作成や本番変更の実施済み報告ではありません。

## 7. 主な根拠への入口

リポジトリ内の相対リンクは閲覧中のbranchを指します。調査時の事実を固定して確認する場合は、[レビュー元commit](https://github.com/halc8312/ESP/tree/72a8e6df47ebd9ec2bf4d4047b4511f325ec4317)を使用してください。

| 分野 | 確認先 |
|---|---|
| 運用契約 | [AGENTS](../AGENTS.md), [Blueprint](../render.yaml), [Dockerfile](../Dockerfile), [worker](../worker.py), [security](../security_config.py) |
| 商品・公開情報 | [models](../models.py), [catalog](../routes/catalog.py), [pricing](../services/pricing_service.py), [auth](../routes/auth.py) |
| 抽出・監視 | [scrape request](../services/scrape_request.py), [scrape tasks](../jobs/scrape_tasks.py), [monitor](../services/monitor_service.py) |
| キュー・画像 | [worker runtime](../services/worker_runtime.py), [image store](../services/bg_remover/job_store.py), [image tasks](../jobs/bg_removal_tasks.py), [image routes](../routes/bg_removal.py) |
| 翻訳・メール | [translator registry](../services/translator/registry.py), [mail service](../services/mail_service.py), [mail CLI](../services/mail_cli.py) |
| 検証 | [CI](../.github/workflows/ci.yml), [Docker CI](../.github/workflows/docker-build.yml), [conftest](../tests/conftest.py), [migration](../services/database_migration.py) |
| 外部一次資料 | [Stanza advisory](https://github.com/stanfordnlp/stanza/security/advisories/GHSA-v5jw-96jm-7h2c), [rembg v2.0.75 dependencies](https://github.com/danielgatis/rembg/blob/v2.0.75/pyproject.toml) |
