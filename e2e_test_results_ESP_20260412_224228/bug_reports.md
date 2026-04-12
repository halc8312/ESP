# 目次
- [検出不具合一覧](#検出不具合一覧)
- [不具合詳細](#不具合詳細)

# 検出不具合一覧
| Bug ID | タイトル | 深刻度 | 優先度 | 状態 |
|---|---|---|---|---|
| BUG-E2E-001 | `single-web-smoke` CLI が CSRF 有効環境でログイン 400 となり完走できない | High | High | Open |
| BUG-E2E-002 | TinyMCE / Chart.js を CDN 直読込しており、到達不能時にコンソールエラーで編集/解析体験が劣化する | Medium | High | Open |
| BUG-E2E-003 | 保護画面リダイレクト後のログインで `next` が無視され、要求画面へ戻れない | Medium | Medium | Open |

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

## BUG-E2E-002
- タイトル: TinyMCE / Chart.js を CDN 直読込しており、到達不能時にコンソールエラーで編集/解析体験が劣化する
- 深刻度: Medium
- 優先度: High
- 事象: `product_detail.html` と `pricelist_edit.html` で TinyMCE、`pricelist_analytics.html` で Chart.js を外部 CDN から直接読込しており、CDN へ到達できない環境で `tinymce is not defined` やチャート読込失敗が発生する。
- 前提条件: アプリへログイン済みで、外部 CDN への接続がブロックまたは一時障害中であること。
- 再現手順:
  1. 商品を1件作成して `/product/1` を開く
  2. 価格表を作成して `/pricelists/1/analytics` を開く
  3. ブラウザコンソールを確認する
- 期待結果: 外部 CDN が利用できなくても画面が graceful degradation し、重大な JS エラーを出さない。
- 実際結果: `tinymce is not defined` が発生し、Chart.js / TinyMCE / Sortable / 外部フォント読込失敗がコンソールへ出力される。
- 再現率: 100%（CDN遮断環境）
- 影響範囲: 商品編集、価格表作成/編集、価格表アクセス解析、オフライン寄り環境、厳格ネットワーク環境
- 暫定回避策: 外部 CDN へ到達可能な環境で利用する。根本回避ではない。
- 原因仮説: テンプレートが CDN スクリプト前提で `tinymce.init(...)` や `new Chart(...)` を呼び、ローカル同梱や代替処理が不足している。
- 追加確認が必要なログ / データ / API: `templates/product_detail.html`, `templates/pricelist_edit.html`, `templates/pricelist_analytics.html` の CDN 読込戦略、CSP/オフライン要件、Sentry 等の本番JSエラーログ

## BUG-E2E-003
- タイトル: 保護画面リダイレクト後のログインで `next` が無視され、要求画面へ戻れない
- 深刻度: Medium
- 優先度: Medium
- 事象: 未ログインで `/dashboard` にアクセスすると `/login?next=%2Fdashboard` に遷移するが、そのまま正しい資格情報でログインしても `/dashboard` ではなく `/` に飛ばされ、`Please log in to access this page.` のフラッシュが残る。
- 前提条件: ログイン済みセッションがない状態。
- 再現手順:
  1. `/logout` でログアウトする
  2. `/dashboard` を直接開く
  3. ログイン画面で正しい資格情報を入力しログインする
- 期待結果: 元の要求先 `/dashboard` に戻るか、少なくとも不要な認証エラー文言が残らない。
- 実際結果: `/` に遷移し、認証エラー由来のフラッシュ `Please log in to access this page.` が表示されたままになる。
- 再現率: 100%
- 影響範囲: セッション切れ復帰、直接URLアクセス、ブックマーク利用、UX 全般
- 暫定回避策: ログイン後に手動で目的画面へ移動する。
- 原因仮説: `routes/auth.py` の login 成功時リダイレクト先が常に `main.index` 固定で、`next` パラメータを読んでいない。
- 追加確認が必要なログ / データ / API: `login_manager.login_view` 利用時の標準挙動との差分、open redirect 防止を含む `next` 検証方針
