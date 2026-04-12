# 目次
- [検出不具合一覧](#検出不具合一覧)
- [不具合詳細](#不具合詳細)

# 検出不具合一覧
| Bug ID | タイトル | 深刻度 | 優先度 | 状態 |
|---|---|---|---|---|
| BUG-E2E-001 | `single-web-smoke` CLI が CSRF 有効環境でログイン 400 となり完走できない | High | High | Open |

# 不具合詳細
## BUG-E2E-001
- タイトル: `single-web-smoke` CLI が CSRF 有効環境でログイン 400 となり完走できない
- 深刻度: High
- 優先度: High
- 事象: 本番前ゲートとして用意された `flask single-web-smoke --mode preview` が、ローカル実運用設定で毎回 `single_web_smoke_login_failed` になり ready=false で終了する。
- 前提条件: `SECRET_KEY` と `DATABASE_URL` を設定し、`flask --app 'app:create_cli_app()' single-web-smoke --mode preview` を実行する。
- 再現手順:
  1. `cd /home/runner/work/ESP/ESP`
  2. `export SECRET_KEY='qa-secret-key'`
  3. `export DATABASE_URL='sqlite:////tmp/esp_cli_smoke.db'`
  4. `flask --app 'app:create_cli_app()' single-web-smoke --mode preview`
- 期待結果: スモークがログイン・ジョブ実行・結果ページ確認まで完了し `ready=true` を返す。
- 実際結果: `/login` への POST が 400 となり `single_web_smoke_login_failed` が返る。
- 再現率: 100%
- 影響範囲: デプロイ前確認、運用Runbook、CLIによる自動ゲート全般
- 暫定回避策: 未確認。CSRF を無効化したテスト環境では再現しないが、本番相当回避策としては不適切。
- 原因仮説: `cli.py` の `run_single_web_smoke()` が Flask test client で `/login` に CSRF token なし POST を送っているため、通常設定の `CSRFProtect` にブロックされる。
- 追加確認が必要なログ / データ / API: Flask 400 レスポンス詳細、CSRF exempt 設計方針、他CLIスモークで同様の POST を行う箇所の横展開調査
