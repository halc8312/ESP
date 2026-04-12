# execution_results

## 目次
- [実行したケース一覧](#実行したケース一覧)
- [証跡一覧](#証跡一覧)
- [事実と仮説の区別](#事実と仮説の区別)

## 実行したケース一覧

| 実行日時 | ケースID | 実行環境 | 実行結果 | 実際結果 | 証跡メモ | 関連不具合ID | 再現性メモ | 未実施理由 | 次回確認ポイント | 事実 / 仮説 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-04-12T23:40:00Z | TC-E2E-RT-001 | pytest | Pass | `tests/test_health_route.py` と `tests/test_auth.py` が通過 | CLI実行ログ | - | 安定 | - | UI実査との整合確認 | 事実 |
| 2026-04-12T23:40:30Z | TC-E2E-RT-002 | pytest | Pass | `tests/test_e2e_routes.py` 78件通過 | CLI実行ログ | - | 安定 | - | 実ブラウザE2Eで重要導線の裏取り | 事実 |

## 証跡一覧
| 証跡ID | 種別 | 概要 | 関連ケース | 備考 |
|---|---|---|---|---|
| EVD-RT-001 | CLIログ | auth/health pytest 5件通過 | TC-E2E-RT-001 | execution_results内に要約記録 |
| EVD-RT-002 | CLIログ | route E2E pytest 78件通過 | TC-E2E-RT-002 | execution_results内に要約記録 |

## 事実と仮説の区別
### 事実
- ローカル環境でpytest実行が可能。
- auth/health/e2e route の既存自動テストは通過した。

### 仮説
- 実ブラウザでの手動E2Eでも主要導線は概ね動作する可能性があるが、未確認。
