# スクレイピング監視の運用記録

実装日: 2026-09-05。対象: 現行のESP split web / worker / PostgreSQL / Redis構成。

## 何を確認できるか

「処理が終了した」「HTTP 200だった」「検索が0件だった」だけではスクレイピング正常としない。
通常利用と既存巡回の結果を受動的に保存する。新しい実サイトへの取得やCAPTCHA回避処理は追加しない。

| 層 | 実行・確認方法 | 確認範囲と限界 |
| --- | --- | --- |
| 既存商品巡回 | worker内、15分間隔、既存の対象商品最大50件 | 対象7サイトの価格・在庫。商品がないサイトや検索経路は検証しない。Record City巡回adapterは未実装 |
| 通常検索・直接取得 | 既存ジョブの終了時 | 8サイトのsearch/detail別に記録。検索はそのジョブの抽出経路であり、直接URL取得の独立成功とはしない |
| 観測・通知の定期評価 | worker内、5分間隔 | DB状態と通知outboxを評価・再送。実サイトへはアクセスしない |
| 保存fixture回帰 | GitHub Actions、毎日03:23 UTC | 保存HTML・mockで回帰を検証。現時点の実サイトのDOMや到達性を保証しない |
| 独立稼働監視 | GitHub Actions、毎時17分UTC、承認済みの有効化commit反映後 | ESP自身の`/stack-readyz`に1回だけ接続。worker・scheduler・巡回・監視ジョブ自身の停止を検出。全サイトの成功証明ではない |

独立稼働監視は、所有者の承認に基づくworkflow設定の変更で、既存webの`https://esp-1-kend.onrender.com`を明示的な既定値として有効化する。
`ESP_MONITOR_BASE_URL`は任意の上書き設定であり、repository variableを登録したという意味ではない。未設定・空値なら承認済み既定値を使う。
停止する場合はrepository variable `ESP_MONITOR_DISABLED=true`を設定する。この場合は **SKIPPED / DISABLED** と表示し、workflowが緑でも本番を監視済みとは扱わない。
ESPへの接続は`halc8312/ESP`のdefault branchだけで実行し、PR・forkでは実行しない。workflowファイル自体がmainへ変更反映されたときにも1回確認する。
GitHubのスケジュールは遅延し得るため、厳密な障害検出SLAではない。
新規Render有料サービスは不要。ただしGitHub Actionsの実行時間を消費する。依存関係を入れるfixture回帰は毎日1回に限定し、契約の無料枠・課金上限は運用者が確認する。

## 管理者の確認場所

- 管理画面 → スクレイピング監視: `/admin/scrape-health`
- サーバー内の読取専用CLI: `flask scrape-health`
- 詳細に確認できていない経路があれば終了コード1: `flask scrape-health --fail-on-warning`

生徒・一般ユーザーは監視画面へアクセス不可。公開カタログや公開health endpointにはサイト別情報を追加しない。
CLIのstrictは未実装のRecord City巡回を含む全24組合せに成功証拠を要求するため、現状では全体正常のゲートには使わない。
日常判断には画面の経路別状態・実装範囲・時刻を使う。

| 表示 | 意味 |
| --- | --- |
| 未観測 | この機能の導入後に実行証拠がない。過去の成功を推測で埋めない |
| 観測なし | 正常な検索0件や判定保留。抽出成功ではなく、以前の失敗も解除しない |
| 直近成功 | 実際に検証した商品がある。タイトル、状態、販売中の有効価格が必要 |
| 一部／単発失敗 | 1回の失敗観測。部分成功があっても全成功にしない |
| 障害継続 | 2回連続の失敗観測でincidentを開く。商品2件ではなく、ジョブまたはサイト別巡回batchを2回 |
| 古い観測 | 実成功・実失敗が24時間超前。現在壊れている証拠ではない。未復旧incidentは別に表示し保持 |
| 監視DB利用不可 | 読取失敗。未観測や正常には置き換えない |

検索結果は利用者の除外・価格フィルター適用前に評価する。意図的に全件除外しても抽出失敗にはしない。
販売終了・削除済み商品は現時点の価格がなくても成立し得る。内部smokeの合成データは実サイト成功として数えない。
DB保存で例外が発生したジョブは`persistence_error`として失敗を返す。保存ポリシーによる意図的な除外は別であり、画面の成功は全商品の保存件数の保証ではない。

既存巡回の`updated_count`はデータ変更件数であり、成功件数ではない。変更なしの有効な結果も`successful_count`へ入れる。
更新・再価格計算・commitが失敗した商品を更新成功に数えない。Mercariのsoft-sold判定保留は成功観測にしない。

## 通知の意味と再送

既存の通知先を再利用する。優先順位は`SCRAPE_ALERT_WEBHOOK_URL`、`SELECTOR_ALERT_WEBHOOK_URL`、`OPERATIONAL_ALERT_WEBHOOK_URL`。
このoutboxの配送履歴・再送は新しい集約監視通知が対象。既存のselector等の即時通知は従来経路のままであり、すべての通知種別の配送履歴を網羅するものではない。
値やCookie、URL、検索語、商品名、利用者IDを監視テーブルに保存しない。環境変数値をログやチャットへ貼らない。
画面とCLIの`scrape_alert_configured`は呼出元プロセスの設定有無だけを示す。webとworkerのenvは独立のため、webの設定ありをworkerの設定・配送成功の証明にしない。

| 状態 | 運用上の意味 |
| --- | --- |
| pending / in_flight | 予約済み／送信処理中 |
| delivered | WebhookのHTTP受理。人が通知を受け取った・読んだ証明ではない |
| unconfigured | 通知先未設定。送信済みではない |
| cooldown / rate_limited | 抑制中。送信済みではない |
| failed | HTTP送信失敗。成功時のcooldownや回数予算を消費しない |
| deferred | 復旧通知の後に単発失敗を観測したため保留。次の実成功で再開、新たな障害が開いたら置換 |
| superseded | 復旧・新しい障害などにより古くなった通知。再送しない |
| exhausted / expired | 送信失敗8回／作成から7日経過。自動再送終了、運用者が確認 |

再送評価は5分間隔、1回最大10件。失敗の再送待ちは10〜60分、未設定は60分。
通知の同時処理はDBのclaim tokenと2分leaseで排他する。古いincident・recoveryの再送は現在の状態と照合して抑止する。
送信中のネットワーク要求は取り消せず、HTTP受理直後のプロセスクラッシュでは重複配信の可能性がある（at-least-once）。厳密なexactly-onceとはしない。

履歴はサイト・経路ごとに観測最大200件、終了済み通知最大100件。30日より古い観測は定期評価で最大1000件ずつ整理する。
観測保存障害は本来の取得ジョブを失敗に巻き込まず、安全な警告ログを出す。その瞬間の観測が欠落する可能性は残るため、テレメトリの完全性は保証しない。

## 本番反映・有効化チェックリスト

このファイルは実装仕様であり、デプロイ・通知受信・独立監視有効化を完了した証明ではない。

1. PRの通常CI、Docker検証、追加のoffline監視workflowを確認する。
2. 既存サービスの同一性を確認し、同じ承認済みcommitをwebとworkerへ反映する。新しいRenderサービスを作らない。
3. 追加migration `20260905_0021`の適用を確認する。新規の監視用3テーブルのみで、商品・利用者データの移行はない。
4. workerログでINFOが維持され、`Scrape health review completed`と巡回件数が記録されることを確認する。in-process AlembicがrootログレベルをWARNへ戻す問題を修正済み。
5. `/readyz`は既存のweb/DB/Redis readinessとして維持する。`/stack-readyz`は独立診断用で、Renderのweb再起動判定に使わない。
6. `/stack-readyz`の`worker`、`scheduler`、`patrol`、`scrape_monitor`を確認する。空巡回は`no_observations`で503、監視評価の完了が15分超前なら`scrape_monitor=stale`。起動直後の初回評価前は未確認が正常。
7. Renderで既存サービスの同一性とweb・workerの反映完了を確認し、独立watchdogを有効化する承認済みworkflow変更をmainへ反映する。既定URLは`https://esp-1-kend.onrender.com`。別URLにする場合だけrepository variable `ESP_MONITOR_BASE_URL`で上書きし、接続先の所有を再確認する。
8. host変更時だけ`ESP_MONITOR_ALLOWED_HOSTS`を設定する。watchdogは許可した正確なRender webホスト、HTTPS、`/stack-readyz`だけを許可し、redirectやqueryやcredentialsを受け付けない。
9. workflow変更のmainへのpushで起動する確認、またはmain上の手動実行で、実際の稼働監視がskipされず実行されたことを確認する。Actions通知を有効にし、受信経路を別途確認する。
10. 通知WebhookのHTTP受理と実際の通知先での受信を確認する。受信未確認なら「通知検証完了」にしない。実サイトをわざと失敗させて試験しない。

ロールバックする場合はweb/workerのコードを戻し、repository variable `ESP_MONITOR_DISABLED=true`で独立watchdogを一時停止する。URL変数を空にしても停止にはならない。再開は`ESP_MONITOR_DISABLED`を削除するか`false`へ変更する。新規テーブルは残して互換性を保つ。
通常のコードロールバックに`alembic downgrade`は不要であり、監視履歴を消すdowngradeは承認なしに行わない。

## 残る境界

- 未利用サイト・未利用経路の実サイトDOM変更を、この受動監視だけで即座に検出することはできない。
- 全サイトを毎日実際に取得するcanaryは未追加。必要なら各サイトで許可された低負荷の経路・対象を別途定義する。CAPTCHA／429には停止し、自動的な回避・連続再試行を追加しない。
- この変更は取得ブラウザ設定や他サイトのfetch経路を変更しない。
- Record Cityのキーワード検索成功は既存の記録を維持するが、導入後の成功日時として遡及登録しない。新しい通常利用の結果で観測を開始する。
