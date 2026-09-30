# 画像・依頼通知導入前コードへの復旧候補（0026を保持）

このブランチは `239f43c6b1d92e59ea4e420e019ed6b4cb1bf2ff` のアプリケーションを保持し、
追加スキーマ0025・0026を認識するためのマイグレーションファイルだけを加えた復旧候補です。
アプリ本体、Productモデル、worker、Render構成は同コミットと同一です。
0025・0026はSQLAlchemy/Alembicだけに依存し、`models_mail` や新しいジョブ実装をimportしません。
テストの期待headは0026です。専用pytest pluginで実際の追加スキーマに対する旧コードの互換性を確認します。

## 適用条件

- `RECORDCITY_LISTING_ENABLED=false` が維持されていること。
- 一覧カード由来の商品、詳細補完状態・ジョブが存在しないこと。
- `product_thumbnail_jobs` と `catalog_request_notifications` に1行も存在しないこと。
- Redis/RQに新しい一覧取得、詳細補完、サムネイル、依頼通知の待機・実行中・予約・再試行ジョブが存在しないこと。
- これらの有無を確認できない場合、この復旧候補を適用しないこと。

このコードには0025の画像ハンドオフ・サムネイル取得や0026の通知outbox処理がありません。
残存する新ジョブの処理や、通知の継続・画像の復旧を引き継げません。
新機能が使用済みの場合は、同機能を保持したコード修正で復旧してください。
データ削除、状態のNULL化、キューの消去で適用条件を作ってはいけません。

0026適用済みDBの読み取り確認例です。本番での実行結果はこの検証には含みません。

```sql
SELECT version_num FROM alembic_version;

SELECT count(*) AS products_with_new_feature_state
FROM products
WHERE detail_fetch_state IS NOT NULL
   OR detail_job_id IS NOT NULL
   OR detail_source_url IS NOT NULL
   OR detail_scope_key IS NOT NULL
   OR detail_lease_expires_at IS NOT NULL
   OR detail_retry_at IS NOT NULL
   OR detail_fail_count IS NOT NULL
   OR detail_error_code IS NOT NULL
   OR detail_translate_requested IS NOT NULL;

SELECT count(*) AS thumbnail_demands FROM product_thumbnail_jobs;
SELECT count(*) AS notification_outbox_rows FROM catalog_request_notifications;
```

revisionは `20260930_0026`、各件数は0が前提です。
DBの件数だけでは、商品保存前の一覧取得やRedis上のジョブを否定できません。
既存の読み取り監視でRQの全キュー、実行中・予約・遅延・失敗からの再試行対象も確認します。
対象には `services.product_detail_jobs.run_product_detail_job`、
`services.product_thumbnail_jobs.run_thumbnail_batch`、
`services.catalog_request_notifications.run_notification_job` と一覧取得のジョブが含まれます。

## 復旧方針

この復旧候補を新しいコミットとしてweb/workerへ反映し、追加されたスキーマは保持します。
旧SHAだけへの巻き戻し、`alembic downgrade`、0024へのstamp、テーブル・列削除は使用しません。
この旧モデルを基準にした `alembic revision --autogenerate` も使用しません。
旧モデルには新しい2テーブルがないため、それらの削除を提案する差分になります。
0024までしか認識しない従来の復旧候補は、0025・0026適用済みDBでは使用できません。

既存の `existing-web-db-migrate` は旧モデルが認識するテーブルだけを対象にします。
新しい2テーブルを含む完全なバックアップ・復元には使えません。DB全体の既存バックアップ方式を維持します。
thumbnail行が残ると旧ショップ削除処理が外部キー制約で失敗するため、行数0の適用条件は省略できません。

反映前に適用条件を確認し、反映後はweb/workerの同一SHA、DB起動ログ、RQ待受、scheduler、巡回を確認します。
本ブランチの作成・ローカル検証・公開だけでは本番への適用は完了しません。

## ローカル検証

次のコマンドは専用の `tests/.tmp/*.db` だけに作用します。
旧モデルのcreate_all結果をテスト内で0024として記録し、実migrationで0025・0026を適用します。
本番DBや任意URLには使用できません。製品の起動処理は変更しません。

```bash
PYTHONPATH=tests python -m pytest -p rollback_0026_plugin \
  tests/test_rollback_0026_compatibility.py tests/test_e2e_routes.py \
  tests/test_worker_entrypoint.py tests/test_worker_runtime.py \
  tests/test_database_bootstrap.py tests/test_database_migration.py \
  tests/test_cli_render_cutover_readiness.py \
  -k 'not test_single_service_web_scheduler_does_not_require_redis_lock' \
  --basetemp=/tmp/esp-rollback-0026-unique-run -q
```

旧single-webのロック判定テストは無関係な実巡回スレッドを起動し、後続テストへ状態が残るため除外します。
同じロック判定とweb起動のDB bootstrapは、0026のfixture上でschedulerだけを止めた専用テストで確認します。
2026-09-30 UTCの検証結果は233件成功、上記の旧ロック判定テスト1件除外でした。
画像容量レビューで0025へnullableな `batch_user_id` が追加された後も、同じmigrationを保持し、
head・列・外部キー・旧商品の保存・web起動を含む専用互換性4件が成功しています。
SQLite上の互換性確認は、本番PostgreSQL・Redisでのreadiness成功や、本番への反映を示しません。
