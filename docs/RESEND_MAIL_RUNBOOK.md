# Resendメール送信基盤

2026-09-12実装。ESPからResendへ1件のメール送信を要求する共通処理と、設定確認・送信確認用CLIを提供する。

## 実装範囲

先方から共有された準備状況は、Resend、認証済みの`jp-items.com`、送信元`noreply@jp-items.com`、同ドメインに限定したSending access APIキー。
現時点の合意記録では、自動送信の用途・宛先・契機は確定していない。そのため、この変更だけで生徒や顧客へ自動メールが送られることはない。

- 生徒登録の仮パスワードは、既存どおり管理画面に一度表示し、管理者から本人に案内する。
- 公開カタログの依頼は、生徒の依頼一覧に保存する既存の運用を継続する。
- スクレイピング監視のWebhook通知は独立した既存機能。Resendへ置き換えない。
- メールによるパスワード再設定、登録案内、依頼通知、メール用の永続outbox、配信イベントWebhookは、この送信基盤には含まない。

送信対象の機能を接続するときに、宛先の決め方、重複防止キーの永続化、再送期限、送信失敗時の画面表示をその機能側で定義する。

## 設定

| 環境変数 | 既定値 | 役割 |
| --- | --- | --- |
| `MAIL_ENABLED` | `false` | 明示的に有効にしたプロセスだけ送信できる。キーを置くだけでは送信しない |
| `MAIL_FROM` | `noreply@jp-items.com` | 認証済みドメイン内の送信元。表示名なしのメールアドレス1件 |
| `RESEND_API_KEY` | 空 | 送信専用・ドメイン限定のキーを、送信するプロセスのSecretとして設定 |

webとworkerの環境変数は独立している。実際に送信するプロセスを決めてからその環境へ設定する。この変更では`render.yaml`やRender環境変数は変更しない。
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

この文書の作成は、本番への設定・送信・受信完了の記録ではない。未確定のメール用途とテスト宛先を決め、コード反映・Secret設定後に実送受信を確認する。
コード反映後も`MAIL_ENABLED=false`なら送信しない。停止時も同じ設定を使う。DBマイグレーションは不要。

## 公式仕様

- [API概要とUser-Agent](https://resend.com/docs/api-reference/introduction)
- [メール送信API](https://resend.com/docs/api-reference/emails/send-email)
- [Sending access APIキー](https://resend.com/docs/create-an-api-key)
- [送信地域](https://resend.com/docs/dashboard/domains/regions)
- [Idempotency-Key](https://resend.com/docs/dashboard/emails/idempotency-keys)
- [エラー分類](https://resend.com/docs/api-reference/errors)
- [送信制限](https://resend.com/docs/api-reference/rate-limit)
- [テスト用メール](https://resend.com/docs/dashboard/emails/send-test-emails)
