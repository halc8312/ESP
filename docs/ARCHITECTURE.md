# 構成と開発時の確認

## アプリケーションの役割

| 領域 | 主な入口 | 責務 |
| --- | --- | --- |
| Web | `wsgi.py`, `app.py`, `routes/` | 認証、管理画面、公開カタログ、画像配信、内部画像アップロード |
| worker | `worker.py`, `services/worker_runtime.py`, `jobs/` | RQの抽出・画像処理、巡回、定期処理の所有 |
| 永続データ | `models.py`, `alembic/` | 利用者・ショップ・商品・価格表・依頼・ジョブ。schema変更はAlembicで管理 |
| 取得 | サイト別 `*_db.py`, `services/patrol/`, `services/marketplace_access.py` | URLと商品同一性の検証、抽出、価格・在庫の巡回、サイト別共有制限 |
| 画像・翻訳 | `services/image_service.py`, `services/bg_remover/`, `services/translator/` | 画像検証・保存、背景除去、翻訳。外部APIを使う設定は別途管理 |
| 画面 | `templates/`, `static/` | 管理画面・公開ページ・ブラウザ側の操作 |
| 運用 | `cli.py`, `scripts/`, `.github/workflows/` | 診断、監視、バックアップ検査、隔離復元、CI |

本番は Web・worker・PostgreSQL・Redis/Valkey の分離構成です。Webの永続ディスクが画像の保管先です。workerだけの一時ファイルを永続画像やバックアップと扱いません。

## 維持する条件

- 公開カタログに仕入れURL・サイト名・内部データを出さない。
- 利用者・ショップ・価格表ごとの認可を保つ。表示を隠すだけで認可の代わりにしない。
- web/workerのDB・Redis・キュー名・署名Secretを一致させる。定期処理の所有者を重複させない。
- 内部画像処理は実ホスト名、内部ポート、HMAC署名を確認する。詳細は [Render手順](RENDER_CUTOVER_RUNBOOK.md)。
- schemaはモデルとAlembicを正とし、DBを直接書き換える使い捨て補助スクリプトを配布しない。
- 本番・顧客のデータや固定アカウントを開発試験へ流用しない。テストのfixtureと使い捨てのローカルDBを使用する。

## 変更に応じた検証

環境構築は [README](../README.md) を参照してください。

```bash
# 画面・認可・利用者の分離
pytest tests/test_e2e_routes.py -q
# workerの起動・定期処理
pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q
# 画像・内部署名
pytest tests/test_image_service.py tests/test_bg_removal_routes.py tests/test_bg_remover_internals.py -q
# 全体の回帰
pytest tests -q
python -m pip check
```

CIでは依存パッケージ監査、通常テスト、Docker起動、PG18の合成データ復元を確認します。既存の依存監査例外は `.github/workflows/ci.yml` に理由が記載されており、依存側の修正版が利用可能になったら見直します。

UI変更では商品編集と手動追加の両方を確認し、デスクトップ・モバイル、画像の追加・削除・並び替え、保存、キーボード操作、ブラウザのエラーも確認します。翻訳欄や編集欄の更新が入力中の値を壊さないことを確認してください。

外部サイトの実動作、メール受信、本番復旧はCIと別に記録します。fixtureの出典は必要に応じて `tests/fixtures/` の説明を保持します。`llama.cpp/` は通常のアプリ変更の対象外です。
