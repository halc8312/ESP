# 目次
- [実行サマリー](#実行サマリー)
- [実行結果一覧](#実行結果一覧)
- [未実施・保留メモ](#未実施保留メモ)
- [次回確認ポイント](#次回確認ポイント)

# 実行サマリー
- 実行開始: 2026-04-12 22:42:28 UTC
- 自動テスト: 既存 route E2E 78件 Pass
- 補助自動テスト: health route 1件 Pass
- ブラウザE2E: 主要導線 8件 Pass、3件 Fail/Issue、1件 Partial
- 重大所見: `single-web-smoke` CLI が CSRF によりログイン 400 で失敗、`next` ログイン遷移不備、CDN依存で `tinymce is not defined`

# 実行結果一覧
| 実行日時 (UTC) | ケースID / 識別子 | 実行結果 | 実際結果 | 証跡メモ | 関連不具合ID | 再現性メモ | 未実施理由 | 次回確認ポイント |
|---|---|---|---|---|---|---|---|---|
| 2026-04-12 22:44 | AUTO-001 `pytest tests/test_e2e_routes.py -x --tb=short` | Pass | 78/78 Pass | EVD-001: CLIログ | - | 100% | - | ブラウザ実地との差分確認 |
| 2026-04-12 22:57 | AUTO-002 `pytest tests/test_health_route.py -q` | Pass | 1/1 Pass | EVD-006: CLIログ | - | 100% | - | 本番 `/healthz` でも再確認 |
| 2026-04-12 22:45 | TC-E2E-040 `single-web-smoke --mode preview` | Fail | `/login` POST が 400、`single_web_smoke_login_failed` | EVD-002: CLIログ | BUG-E2E-001 | 100% | - | CSRF 前提での修正要否確認 |
| 2026-04-12 22:47 | TC-E2E-001, 002 | Pass | 新規登録後に商品一覧へ遷移し、再ログインも成功 | EVD-003, Playwright snapshot | - | 100% | - | ログアウト後戻るの厳密確認を別環境で追加 |
| 2026-04-12 22:48 | TC-E2E-003 | Pass | ショップ作成と current shop 切替が即時反映 | Playwright snapshot | - | 100% | - | 複数ショップ切替時の一覧スコープも追試 |
| 2026-04-12 22:49-22:50 | TC-E2E-004, 021, 022 | Pass | 手動商品登録後、商品詳細と一覧に価格・英語タイトル・在庫が反映。更新後タイトルも一覧へ反映 | Playwright snapshot | - | 100% | - | バリエーション追加/削除の追試 |
| 2026-04-12 22:50 | TC-E2E-039 | Fail | 商品編集画面で `tinymce is not defined`、外部 CDN 読込失敗コンソールエラーを確認 | Playwright console, template参照 | BUG-E2E-002 | 100% | - | CDN遮断時の graceful degradation 設計確認 |
| 2026-04-12 22:51-22:52 | TC-E2E-007, 008, 028 | Pass | 価格表作成、商品追加、公開カタログ表示、備考の `<script>` 除去、`source_url`/内部サイト情報の非露出を確認 | Playwright snapshot/evaluate | - | 100% | - | 実本番データで token 失効/権限境界を追試 |
| 2026-04-12 22:52 | TC-E2E-036 | Pass | 公開カタログは 390px 幅でも主要情報を表示し操作不能にはならない | EVD-004 | - | 100% | - | 実機 Safari/Android で再確認 |
| 2026-04-12 22:52 | TC-E2E-039 (analytics) | Partial | アクセス解析のテーブル系集計は表示されたが、Chart.js CDN 読込失敗コンソールエラーあり | Playwright console, analytics page | BUG-E2E-002 | 100% | - | グラフ描画のフォールバック確認 |
| 2026-04-12 22:53 | TC-E2E-027 | Pass | ログアウト後 `/dashboard` 直アクセスで `/login?next=%2Fdashboard` にリダイレクト | Playwright snapshot | - | 100% | - | 他保護ルート横展開 |
| 2026-04-12 22:54 | EXP-LOGIN-NEXT | Fail | 保護画面からログインすると `next=/dashboard` が無視され `/` へ遷移し、`Please log in to access this page.` が残留 | Playwright snapshot | BUG-E2E-003 | 100% | - | `next` を使う全認証導線の回帰確認 |
| 2026-04-12 22:55 | TC-E2E-014 | Partial | 仕入価格 `-1` の送信は画面遷移せず登録もされなかったが、明示的エラーメッセージは確認できず | Playwright snapshot | - | 100% | - | サーバ側エラー文言の明示有無を追加確認 |

# 未実施・保留メモ
| 項目 | 状態 | 理由 | 代替確認 |
|---|---|---|---|
| 実外部サイトとのライブスクレイプ | Not Run | ネットワーク依存・本番影響考慮 | fixture/既存自動テストで代替 |
| Redis/RQ フルE2E | Not Run | ローカル Redis 未確認 | `tests/test_rq_scrape_e2e.py` のスキップ条件確認 |
| Safari/iOS/Android 実機 | Not Run | 利用可能ブラウザ制約 | Chromium responsive で入口確認 |
| マルチユーザーIDOR | Blocked | 追加ユーザー/データを別セッションで準備していない | 既存 route E2E とコード読解で代替 |

# 次回確認ポイント
- `next` パラメータを使う認証導線修正後の回帰確認
- CDN 到達不能時の TinyMCE / Chart.js フォールバック実装有無確認
- マルチユーザーIDOR、実機ブラウザ、外部連携失敗時の実地確認
