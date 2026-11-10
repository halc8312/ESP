# ESP ドキュメント案内

現行の仕様・運用手順の入口です。実装済みの機能と、本番での有効化・受け入れ確認は区別しています。

| 用途 | 文書 |
| --- | --- |
| 製品概要・ローカル起動 | [README](../README.md) |
| 初回納品の残作業・確認項目 | [DELIVERY_STATUS](DELIVERY_STATUS.md) |
| 構成・開発時の制約・検証 | [ARCHITECTURE](ARCHITECTURE.md) / [AGENTS](../AGENTS.md) |
| 構成台帳・設定名・バックアップと復元 | [OPERATIONS_HANDOVER](OPERATIONS_HANDOVER.md) |
| Render の分離構成と反映時の確認 | [RENDER_CUTOVER_RUNBOOK](RENDER_CUTOVER_RUNBOOK.md) / [render.yaml](../render.yaml) |
| 2段階取得の実装範囲と制約 | [TWO_STAGE_EXTRACTION](TWO_STAGE_EXTRACTION.md) |
| 公開カタログ・Add・依頼一覧 | [CATALOG_REQUESTS](CATALOG_REQUESTS.md) |
| メールの有効化と受信確認 | [RESEND_MAIL_RUNBOOK](RESEND_MAIL_RUNBOOK.md) |
| 巡回の停止・修正・再開 | [PATROL_OPERATIONS](PATROL_OPERATIONS.md) |
| 監視の見方と通知 | [SCRAPE_MONITORING_RUNBOOK](SCRAPE_MONITORING_RUNBOOK.md) |
| RecordCity の取得設定・制限時の診断 | [RecordCity 運用](recordcity-waf-reachability.md) |
| 性能計測 | [ARC1_KPI_RUNBOOK](ARC1_KPI_RUNBOOK.md) |
| CSV の列仕様 | [Shopify CSV](shopify_csv_structure.md) |
| 元の画面要件 | [PRODUCT_BRIEF](requirements/PRODUCT_BRIEF.md) |
| セキュリティ・漏えい対応 | [SECURITY](../SECURITY.md) / [INCIDENT_RESPONSE](../INCIDENT_RESPONSE.md) |
| ライセンス整理・公開前の文面確認 | [LICENSE_PENDING](../LICENSE_PENDING.md) / [利用規約の草案](legal/TERMS_OF_SERVICE_DRAFT.md) / [プライバシーポリシーの草案](legal/PRIVACY_POLICY_DRAFT.md) |

[SINGLE_WEB_REDEPLOY_RUNBOOK](SINGLE_WEB_REDEPLOY_RUNBOOK.md) と `render.existing-web-addons.yaml` は旧構成・初回移行の互換資料です。現在の分離構成へ無条件に適用しないでください。

完了済みの作業報告、重複した計画、特定の開発環境専用の補助手順は整理しました。変更経緯は Git・PR・レビューの履歴から確認できます。個別の接続情報、認証情報、連絡先は非公開の運用台帳で管理します。
