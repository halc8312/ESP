# 目次
- [実行サマリー](#実行サマリー)
- [実行結果一覧](#実行結果一覧)
- [未実施・保留メモ](#未実施保留メモ)
- [次回確認ポイント](#次回確認ポイント)

# 実行サマリー
- 実行開始: 2026-04-12 22:42:28 UTC
- 自動テスト: 既存 route E2E 78件 Pass
- ブラウザE2E: 実施中
- 重大な初期所見: `single-web-smoke` CLI が CSRF によりログイン 400 で失敗

# 実行結果一覧
| 実行日時 (UTC) | ケースID / 識別子 | 実行結果 | 実際結果 | 証跡メモ | 関連不具合ID | 再現性メモ | 未実施理由 | 次回確認ポイント |
|---|---|---|---|---|---|---|---|---|
| 2026-04-12 22:4x | AUTO-001 `pytest tests/test_e2e_routes.py -x --tb=short` | Pass | 78/78 Pass | EVD-001: CLIログ | - | 100% | - | ブラウザ実地との差分確認 |
| 2026-04-12 22:4x | TC-E2E-040 `single-web-smoke --mode preview` | Fail | `/login` POST が 400、`single_web_smoke_login_failed` | EVD-002: CLIログ | BUG-E2E-001 | 100% | - | CSRF 前提での修正要否確認 |

# 未実施・保留メモ
| 項目 | 状態 | 理由 | 代替確認 |
|---|---|---|---|
| 実外部サイトとのライブスクレイプ | Not Run | ネットワーク依存・本番影響考慮 | fixture/既存自動テストで代替 |
| Redis/RQ フルE2E | Not Run | ローカル Redis 未確認 | `tests/test_rq_scrape_e2e.py` のスキップ条件確認 |
| Safari/iOS/Android 実機 | Not Run | 利用可能ブラウザ制約 | Chromium responsive で入口確認 |

# 次回確認ポイント
- ブラウザでの正常系/異常系実測を追記
- 公開カタログからの内部情報露出有無を証跡付きで記録
- ログアウト後戻る、直接URL、モバイル表示を実測反映
