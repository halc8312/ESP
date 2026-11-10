# ESP ドキュメント案内

現行の運用・引き継ぎでは、次の順に確認してください。文書上の実装状況と、本番での有効化・実測結果は別に確認します。

| 用途 | 文書 |
| --- | --- |
| 製品概要・開発環境 | [README](../README.md) |
| 本番の構成・設定名・バックアップ・復元 | [OPERATIONS_HANDOVER](OPERATIONS_HANDOVER.md) |
| 2段階取得の実装範囲と受け入れ前の制約 | [TWO_STAGE_EXTRACTION](TWO_STAGE_EXTRACTION.md) |
| Render の構成定義 | [render.yaml](../render.yaml) |
| 開発時に守る制約・検証 | [AGENTS](../AGENTS.md) |
| セキュリティ連絡・事故対応 | [SECURITY](../SECURITY.md) / [INCIDENT_RESPONSE](../INCIDENT_RESPONSE.md) |

## 過去の設計・試験記録

以下は作成時点の記録です。未着手・完了の記載を、そのまま現在の状態と扱わないでください。

- [旧 README](archive/README_20260930.md): 更新前の説明全体。現在とは異なる起動方法や構成説明を含みます。
- [初回 split 移行の計画](RENDER_CUTOVER_RUNBOOK.md): single-web からの移行時の手順。
- [single-web の再配置手順](SINGLE_WEB_REDEPLOY_RUNBOOK.md): 旧構成・互換確認用。
- [商品編集リデザインの引き継ぎ](handoff/README.md): 2026年4月時点の開発計画・テスト記録。
- [過去のセキュリティ調査](../SECURITY_CHECK_REPORT.md): 当時の指摘。現行実装の再確認が必要です。
- [外部サービスの機能調査](../deepbay_requirements.md): 参考調査であり、ESP の納品仕様ではありません。

これらの記録と、PR・コミット・レビューの履歴は、変更理由を追えるよう保持しています。
