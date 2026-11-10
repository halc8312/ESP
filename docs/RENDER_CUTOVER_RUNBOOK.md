# Render 分離構成の確認手順

本番の基準は Web・worker・PostgreSQL・Redis/Valkey の分離構成です。通常の反映では既存Web・DB・画像ディスクを作り直しません。環境の台帳、バックアップ、復元、権限引き渡しは [OPERATIONS_HANDOVER](OPERATIONS_HANDOVER.md) を参照してください。

## 構成ファイルの使い分け

- [render.yaml](../render.yaml): 分離構成の参照定義。新しい専用環境ではリソース・プラン・ディスク・設定を確認して使用する。
- [render.existing-web-addons.yaml](../render.existing-web-addons.yaml): 旧Webを維持した初回移行用の追加定義。Webを含まないため、既に分離済みの環境へ重ねて適用しない。
- [SINGLE_WEB_REDEPLOY_RUNBOOK](SINGLE_WEB_REDEPLOY_RUNBOOK.md): single-webの互換確認用。現在のworkerやRQを無効にする指示として扱わない。

Blueprintのプラン・容量は実環境の利用量を示しません。Dashboardで実サービス名、契約プラン、DBと画像ディスクの確保容量・使用量、環境変数の登録数を確認し、Secretの値を公開資料へ記載しないでください。

## web / worker の共有条件

| 設定 | 確認事項 |
| --- | --- |
| Git SHA・schema | 両サービスで同じ検証済みreleaseを使い、Alembic revisionと互換性を確認する |
| `SECRET_KEY`, `BG_REMOVAL_INTERNAL_SECRET` | web/workerで同じ署名設定を使う。`sync: false`は同値の自動配布を保証しない |
| `DATABASE_URL`, `REDIS_URL` | 同じDB・キュー・ロック・heartbeatを参照する |
| `SCRAPE_QUEUE_BACKEND` | 分離構成では `rq`。一時回避として無断で `inmemory`へ切り替えない |
| scheduler | Web側は無効。専用workerが定期処理を所有する |
| `IMAGE_STORAGE_PATH` | Webの永続領域。workerの一時領域で代替しない |
| `WEB_INTERNAL_HOST`, `WEB_INTERNAL_PORT` | DashboardのInternal接続先または `fromService.property: host` を使う。論理サービス名から実ホスト名を推定しない。内部ポートの既定値は8080 |

内部URLは `ESP_WEB_INTERNAL_URL` / `WEB_INTERNAL_URL` / `WEB_PUBLIC_URL` がhost・portより優先されます。古い明示URLが残っていないか確認します。内部アップロードの `401 invalid_signature` は共有Secret、名前解決失敗は実ホスト名、接続失敗はbind・ポート・同じregion/workspaceへの所属を確認します。

Dockerfileは公開ポートと内部ポート8080へbindします。このプロジェクトの配線を維持してください。Renderのポート制限は変わるため、判断には [公式Private Network資料](https://render.com/docs/private-network) を使います。過去の「10000は常に内部通信不可」という前提を流用しません。

## 反映前の確認

新規ローカル・検証環境で、対象DBと設定を確認して実行します。

```bash
flask --app app:create_cli_app render-blueprint-audit
flask --app app:create_cli_app render-local-split-checklist --blueprint-path render.yaml
flask --app app:create_cli_app render-local-split-readiness
pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q
pytest tests/test_bg_removal_routes.py tests/test_bg_remover_internals.py -q
```

次のコマンドはDBへmigrationを書き込みます。まず隔離したPostgreSQLで検証します。本番ではバックアップと実行担当・反映計画を確認したうえで実施してください。

```bash
flask --app app:create_cli_app render-cutover-readiness --require-backend postgresql --apply-migrations --strict
```

通常CI・Docker検証・PG18復元CIの結果を対象SHAで確認します。Blueprintの差分が既存サービス・DB・ディスクの作り直しを含まないか確認します。コードpushだけでDashboardの設定も更新されたとはみなしません。

## 反映後の確認と障害対応

1. web/workerの同一SHA、schema、永続ディスクの維持を確認する。
2. `/readyz`でDB/Redis、`/stack-readyz`でworker・scheduler・巡回完了・監視評価を確認する。後者をWeb再起動用health checkへ置き換えない。
3. workerのRQ待受、巡回の成功・失敗・停止、滞留ジョブを確認する。
4. 管理画面と公開カタログで画像・利用者分離・取得元非公開を確認する。
5. [RecordCity設定](recordcity-waf-reachability.md)、[監視](SCRAPE_MONITORING_RUNBOOK.md)、[メール](RESEND_MAIL_RUNBOOK.md) を環境ごとに確認する。

コードを戻す場合はDBの前方互換性を確認します。通常の障害対応でDBを逆migrationしたり、古いdumpで本番を上書きしたりしません。selector修復の適用、メール送信、外部有料providerの有効化は通常の反映にまとめず、それぞれ対象と影響を確認します。
