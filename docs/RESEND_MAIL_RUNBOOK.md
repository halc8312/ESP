# Resendメール送信基盤

2026-09-12に送信基盤を実装。2026-09-30に、公開カタログの新しい依頼を所有者へ知らせる永続outboxを追加した。どちらも既定で無効。本番の設定・送信・受信確認は、この文書の更新では実施していない。

## 実装範囲

先方から共有された準備状況は、Resend、認証済みの`jp-items.com`、送信元`noreply@jp-items.com`、同ドメインに限定したSending access APIキー。
送信基盤だけを導入した2026-09-12時点では、自動送信の用途・宛先・契機は確定していなかった。追加した依頼通知は、公開カタログから保存に成功した新規依頼について、所有者の登録済み`User.email`へ1通を通知する。顧客への返信・パスワード案内は含まない。

- 生徒登録の仮パスワードは、既存どおり管理画面に一度表示し、管理者から本人に案内する。
- 公開カタログの依頼は、生徒の依頼一覧に保存する。通知を無効にした状態でも保存と受付結果は利用できる。
- スクレイピング監視のWebhook通知は独立した既存機能。Resendへ置き換えない。
- メールによるパスワード再設定、登録案内、配信イベントWebhookは含まない。

送信対象の機能を接続するときに、宛先の決め方、重複防止キーの永続化、再送期限、送信失敗時の画面表示をその機能側で定義する。

## 設定

| 環境変数 | 既定値 | 役割 |
| --- | --- | --- |
| `MAIL_ENABLED` | `false` | 明示的に有効にしたプロセスだけ送信できる。キーを置くだけでは送信しない |
| `CATALOG_REQUEST_NOTIFICATIONS_ENABLED` | `false` | 新規依頼の通知を別に許可。`MAIL_ENABLED`だけでは依頼通知を開始しない |
| `MAIL_PROVIDER` | 空 | 依頼通知では`resend`を明示。SMTPへ自動で切り替えない |
| `MAIL_FROM` | `noreply@jp-items.com` | 認証済みドメイン内の送信元。表示名なしのメールアドレス1件 |
| `RESEND_API_KEY` | 空 | 送信専用・ドメイン限定のキーを、送信するプロセスのSecretとして設定 |

webとworkerの環境変数は独立している。依頼通知ではwebがoutboxを作成し、RQのmediaキューを消費するworkerが送信するため、両方に同じ許可フラグ・provider・送信元・キーを設定する。`SCRAPE_QUEUE_BACKEND=rq`、共有DB/Redis、同じ`MEDIA_QUEUE_NAME`が必要。この変更では`render.yaml`やRender環境変数は変更しない。
キーや期限付き共有URLを、git・PR本文・ログへ保存しない。

TokyoはResend側のドメイン送信地域であり、APIのURLは`https://api.resend.com/emails`のまま。認証済みDNSやRenderリージョンの変更は不要。
Sending accessキーはメール送信専用なので、ドメイン一覧や送信済みメールをGETする検証は使わない。

## ローカル設定の確認

```bash
flask mail-check
flask mail-test --to operator@example.com --delivery-key esp-mail-test-v1-20260912-a
```

`operator@example.com`は説明用の例。実運用では、送信テスト用に指定された宛先へ置き換える。
上記の2コマンドはResendへ接続しない。`mail-check`の`ready`はローカル設定の成立で、キーの権限・ドメイン認証・受信を検証した意味ではない。
`mail-test`は既定で`dry_run`。設定が未準備または入力が不正なら終了コード2となる。
出力にはキー、宛先、本文、Resendの生のエラー本文を含めない。

## 実送信の確認

送信元・宛先・テスト実施が決まった後、送信するプロセスで`MAIL_ENABLED=true`を設定し、次を1回実行する。

```bash
flask mail-test --to operator@example.com --delivery-key esp-mail-test-v1-20260912-a --send
```

CLIの確認メールは固定本文で、パスワード・商品情報を含まない。成功時の終了コード0と`accepted`はResend APIが受け付けた証拠で、受信箱への到達ではない。
受信者が実際のメールを確認した結果を別に記録する。迷惑メール振り分けやバウンスも、APIの受理だけでは確定できない。

Resendのテスト用宛先も利用できるが、送信枠を消費し、実際の利用者の受信確認にはならない。

## 送信結果と再試行

HTTP処理は既存の`requests`を利用し、固定のResend APIへ1回だけ送る。認証・JSON・User-Agent・Idempotency-Keyを指定し、接続3.05秒・読取10秒のタイムアウトを設ける。リダイレクトとHTTP層の自動リトライは使わない。

| 設定・送信結果 | 対応 |
| --- | --- |
| `disabled` / `unconfigured` / `configuration_error` | 送信前に停止。設定を確認する |
| `accepted` | API受理。受信確認を別途行う |
| `rejected` | 宛先・権限・送信元・利用枠・入力等を確認する。キーを変えて自動再送しない |
| `retryable` | 短時間の制限や同じ送信の処理中。Retry-Afterを尊重し、同じキー・同じ本文で再試行する |
| `unknown` | タイムアウト、5xx、不完全な成功応答等。実際には受理済みの可能性がある |

Idempotency-Keyはメール1件につき固定し、同じ宛先・送信元・件名・本文を維持する。Resendの重複抑止は24時間で、永続的な保証ではない。
初回試行時刻とキー・送信内容は呼出元の責任で保持し、結果不明のメールを24時間経過後に自動再送しない。
このCLIは再送台帳を保存しないため、運用者が試行時刻と結果を記録する。確認メール本文を将来変更した場合は、旧試行と同じキーで送らない。
日次・月次枠の超過は短時間のレート制限と区別する。Idempotency-Keyと本文の不一致も自動再試行しない。

## 検証と有効化前の残件

```bash
python -m pytest tests/test_mail_service.py tests/test_cli_mail.py -q
```

テストはHTTPを置き換え、実メールを送らずに設定、API契約、認証エラー、レート制限、重複キー、通信切断、CLIの無送信動作を検証する。

2026-09-12のローカル検証では送信処理76件、CLI13件が成功。認証・生徒管理・商品依頼・利用者間の分離・設定の回帰を合わせた213件も、Pythonのネットワーク接続を禁止した状態で成功した。

この文書の作成は、本番への設定・送信・受信完了の記録ではない。テスト宛先を決め、コード反映・Secret設定後に実送受信を確認する。
コード反映後も`MAIL_ENABLED=false`なら送信しない。依頼通知だけを停止する場合は`CATALOG_REQUEST_NOTIFICATIONS_ENABLED=false`を両プロセスへ反映する。依頼通知の追加にはAlembic `20260930_0026`（親`0025`）が必要。既存依頼から通知行を作るバックフィルは行わない。

## 依頼通知の保存・回復

新規依頼と通知1行を同じDBトランザクションで保存する。`request_id + request_created`の一意制約により、同じ受付キーの再送や複数商品の依頼でも通知は1行となる。コミット後にRQへ投入し、キュー障害で保存済み依頼の受付結果を失敗に変えない。通知は公開カタログのレスポンスへ含めない。

依頼作成時に、正規化した所有者のメール、送信元、件名、固定本文と冪等キーを凍結する。本文は受付番号と依頼一覧の確認案内だけで、仕入先・商品URL・原価・顧客名・顧客メッセージを含めない。送信直前にも所有者の利用停止状態・依頼の所有者・現在の登録メールを再確認し、宛先または送信元が変わった行は`cancelled`で停止する。所有者・依頼を同じ順序でlockし、現在のメール・利用状態・所有者・凍結内容を条件にした`queued→running`の更新が送信への引き渡し時点となる。HTTP通信中はDB lockを持たない。引き渡し後のメール変更やフラグ停止で、開始済みの通信を取り消したと扱わない。別のメールアドレスへ古い依頼通知を転送しない。

| 状態 | 意味と確認 |
| --- | --- |
| `disabled` | 作成時または再試行時に許可フラグが無効。後から有効にしても自動再送しない |
| `unconfigured` | provider・キー・送信元・所有者メールが未準備。設定修正後も古い行を自動再送しない |
| `pending` / `queued` / `running` | 送信待ち・RQ投入済み・試行中。期限とclaimを確認する |
| `accepted` | Resend API受理。受信未確認。配信イベントWebhookは未実装 |
| `rejected` / `cancelled` | API拒否、利用停止、宛先・送信元変更など。原因を確認し、新しいキーへ置き換えない |
| `manual_review` / `exhausted` | 冪等保証の期限または試行上限で停止。自動再送しない |

RQの状態を見直す目安は120秒、APIの送信試行は最大8回。通常のキュー待機が長くても、同じclaimのjobを新規発行しない。DBトランザクションの外でRQを確認し、`queued/deferred/scheduled/started`なら同じclaimを維持、Redisの結果が不明なら再投入せず容量を保持する。存在しない／終了したjobだけを回復対象とする。キュー投入・失われたclaimの再投入は別に最大16回で、送信枠とは別に数える。所有者あたり同時10件、全体50件までとし、上限待機は試行数に含めない。起動時と既存の5分回復処理で最大20行ずつ確認する。Redisの古いジョブを一括復元しない。

`retryable`/`unknown`とクラッシュ後の回復は同じ冪等キー・同じ本文を使用し、Retry-Afterと段階的な待機を守る。初回API試行から23時間以内に限り、次の待機がその窓を越える場合も停止する。これはResendがキーを24時間保持する仕様に対する余裕であり、永続的な重複抑止や配達保証ではない。[公式仕様](https://resend.com/docs/dashboard/emails/idempotency-keys)を2026-09-30に確認した。

## 依頼通知の安全な有効化確認

最初は両プロセスで依頼通知を無効にしたまま、送信元とテスト宛先を確認する。`MAIL_ENABLED`だけを有効にしてCLIの確認メールを試しても、依頼通知は開始しない。受信確認を記録するまで「メール連携完了」としない。

```bash
flask mail-check
flask mail-test --to operator@example.com --delivery-key esp-mail-check-v1-20260930-a
flask catalog-notification-status
flask catalog-notification-status --user-id 1
```

これらは読取・dry-runで、実送信しない。最後の2コマンドは設定の真偽と状態別件数だけを出し、宛先・本文・キーを表示しない。`--user-id`はDB上の確認済み所有者IDを指定するための管理CLI引数であり、公開APIではない。

指定したテスト宛先での送受信確認、所有者の登録メール、RQと共有設定を確認した担当者が、両プロセスの`CATALOG_REQUEST_NOTIFICATIONS_ENABLED=true`を明示的に反映する。その後に作成した新しいテスト依頼1件について、保存・1通知・API受理・受信を別々に確認する。無効時の過去依頼を通知するSQL更新や一括バックフィルは、この有効化手順に含めない。

## バックアップと残る証拠

outboxは依頼と一緒にDBバックアップへ含める。復元試験では両メールフラグを無効にし、外部通信を遮断した別環境を使う。復元後に古い`running`/結果不明の行を自動で送る前に、初回試行時刻と現在の宛先・凍結内容を確認する。[運用引き継ぎ手順](OPERATIONS_HANDOVER.md)を参照する。

ローカル試験はAPI transportをmockして行った。本番Secret・provider設定、実送信、受信箱到達、バウンス、過去outboxの復元リハーサルは未確認である。

## 公式仕様

- [API概要とUser-Agent](https://resend.com/docs/api-reference/introduction)
- [メール送信API](https://resend.com/docs/api-reference/emails/send-email)
- [Sending access APIキー](https://resend.com/docs/create-an-api-key)
- [送信地域](https://resend.com/docs/dashboard/domains/regions)
- [Idempotency-Key](https://resend.com/docs/dashboard/emails/idempotency-keys)
- [エラー分類](https://resend.com/docs/api-reference/errors)
- [送信制限](https://resend.com/docs/api-reference/rate-limit)
- [テスト用メール](https://resend.com/docs/dashboard/emails/send-test-emails)
