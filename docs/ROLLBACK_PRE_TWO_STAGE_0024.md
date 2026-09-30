# 二段階取得導入前コードへの復旧候補（0024を保持）

このブランチは `353712f51307ef47045d95c413e46fd6a4c4708e` のアプリケーションに、
`20260930_0024` の追加マイグレーションだけを残した復旧候補です。
アプリ本体・worker・Render構成は同コミットと同一です。
テストの期待headを0024へ更新し、実際の0024スキーマで旧コードを検証する明示的なpytest pluginを追加しています。

## 適用条件

- `RECORDCITY_LISTING_ENABLED=false` が維持され、二段階取得が本番でまだ使用されていないこと。
- 一覧カード由来の商品、詳細補完の状態、一覧取得・詳細補完の新ジョブが存在しないこと。
- 新機能のデータやジョブの有無が不明な場合は、この復旧候補を適用しないこと。

旧コードのProductモデルと公開カタログ・巡回処理は `detail_fetch_state` 等を認識しません。
pending/failedを含む未検証商品の保護や、新しい詳細補完ジョブの処理を引き継げません。
一覧由来商品が存在する、または存在を否定できない場合は、新コードを保った修正で復旧します。
DB列を削除したり、状態をNULLに書き換えたりして適用条件を満たしてはいけません。

0024適用済みDBでの読み取り確認例です。実行結果はこの検証では取得していません。

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
```

revisionは `20260930_0024`、件数は0が前提です。
この件数だけでは、商品未保存の一覧取得ジョブやRedis内の待機・実行中ジョブを否定できません。
既存の読み取り監視で新機能のジョブがないことも確認してください。

## 復旧方針

この候補を新しいコミットとして反映し、0024を知るコードで起動します。
単純に旧SHAへ戻すと、旧AlembicチェーンがDBの0024を認識できないため起動に失敗する可能性があります。
`alembic downgrade` や0023へのstamp、列削除は使用しません。

反映前に上記適用条件を確認し、web/workerの起動・同一SHA・DB起動ログ・RQ待受・scheduler・巡回を再確認します。
本ブランチの作成とローカル検証だけでは、本番への適用は完了していません。

## ローカル検証（2026-09-30 UTC）

実際の0024 migrationで追加した9個のnullable列とindexを持つSQLite DBを使用しました。
旧モデルによる画面・商品保存・worker動作・DB bootstrap・migration・readiness CLIの契約を検証しています。

```bash
PYTHONPATH=tests python -m pytest -p rollback_0024_plugin \
  tests/test_e2e_routes.py tests/test_worker_entrypoint.py \
  tests/test_worker_runtime.py tests/test_database_bootstrap.py \
  tests/test_database_migration.py tests/test_cli_render_cutover_readiness.py \
  --basetemp=/tmp/esp-rollback-0024-unique-run -q
```

pluginはtest app専用の `tests/.tmp/*.db` にだけ作用します。
旧モデルのcreate_all結果をテスト内で0023としてstampし、実migrationで0024へupgradeします。
本番DBや任意URLには使用できません。製品の起動処理は変更しません。

230ケースを確認しました。初回一括実行は229成功・1失敗でした。
旧 `test_single_service_web_scheduler_does_not_require_redis_lock` が実schedulerを起動したままにするため、
後続のheartbeatテストへ `patrol_started` が混入したことを確認しました。
worker群をこの1ケースから分離して再実行し、95成功＋同ケース単独1成功でした。
本番の巡回は実行していません。

実際の `run_render_cutover_readiness(..., require_backend="sqlite", apply_migrations=True, strict=True)` も、
別のローカルDBと存在しないloopback Redisだけを指定して実行しました。

- DB headは0024、schema driftはready。
- Blueprint・構成予算audit、parser、single-webのfixture保存は通過。
- 全体のreadyはfalse。split構成が要求するPostgreSQLがなく、Redisへのローカルsocket接続も許可されないため。
- Redisが必要な2つのstack smokeとworker healthは未確認。

これは旧コードと追加スキーマのローカル互換性の確認です。
本番PostgreSQL・Redisでの完全なreadiness成功や、既存のSNKRDUNK残存不具合の解消を示すものではありません。
