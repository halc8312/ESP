# ESP — 商品管理・公開カタログ

ESP は、国内の仕入れサイトから商品を取り込み、価格・画像・翻訳を管理して、海外のお客様向けの価格表と公開カタログを作る Web アプリケーションです。

## 主な機能

- 商品URL・一覧URL・検索からの抽出、プレビュー確認、商品登録
- 商品・ショップ・販売価格・画像・英語テキストの管理
- 公開カタログ、Quick View、在庫に応じた Add 表示、依頼一覧
- 登録商品の価格・在庫の巡回、失敗・停止・再試行の確認
- Shopify / eBay 向けCSVなどのエクスポート

実装の存在と、本番での有効化・各サイトでの動作確認は区別しています。サイト側の表示変更やアクセス制限により、取得できない場合があります。

## 引き継ぎ・運用資料

[ドキュメント案内](docs/README.md) を起点に確認してください。

| 確認すること | 資料 |
| --- | --- |
| 本番構成、設定名、バックアップ・復元 | [運用引き継ぎ](docs/OPERATIONS_HANDOVER.md) |
| 2段階取得の実装範囲・制約 | [2段階取得](docs/TWO_STAGE_EXTRACTION.md) |
| Render の構成定義 | [render.yaml](render.yaml) |
| 開発上の制約・テスト方針 | [AGENTS.md](AGENTS.md) |
| セキュリティ連絡先・対応 | [SECURITY.md](SECURITY.md) / [事故対応手順](INCIDENT_RESPONSE.md) |

## 本番の構成

Web、専用worker、PostgreSQL、Redis/Valkey を分離する構成です。Web が画面・公開カタログ・永続画像を提供し、worker が抽出キュー・巡回・定期処理を担当します。

- `worker.py` が専用workerの起動点です。定期処理の所有者はworkerです。
- Webとworkerで、DB・Redis・署名用Secretなどの設定を一致させます。
- 画像の永続保存先はWebのディスクです。workerの一時保存先だけではバックアップになりません。
- `render.yaml` は構成の参照です。既存サービスの実際の名称・プラン・容量・環境変数はRenderで確認します。
- single-web 用の文書・コマンドは旧構成の互換確認用です。

公開カタログには、仕入れURL・仕入れサイトなどの内部情報を表示しません。変更時は利用者・ショップ・価格表の分離を維持してください。

## 初回納品に向けた確認事項

2026-11-10 のソース確認時点では、次を完了扱いにしていません。

- **数百件の2段階取得**: RecordCityの一覧取得は既定で無効の試作段階です。実際の絞り込み一覧・ページ送り・300〜500件での受け入れ確認が必要です。他サイトの一覧専用アダプターも別途対応が必要です。
- **巡回**: サイト別の処理・監視はありますが、実際の登録商品で価格・在庫更新と失敗回復を確認する必要があります。全商品が一定時間内に一巡することは保証していません。
- **メール通知**: 実装は既定で無効です。送信先を決めた送信・受信確認が必要です。
- **復旧**: 合成データによる復元試験と手順書があります。本番データ・画像を用いた復元試験、担当者・保存先・復旧目標の確定が必要です。
- **引き渡し**: GitHubの権限、専用Render環境、構成・容量の実測一覧を確認します。

## ローカル開発

Python 3.11以降を使用します。以下は新しいローカルDB用の手順です。本番の接続先・APIキーを流用しないでください。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
scrapling install
patchright install chromium

export APP_ENV=development
export RUNTIME_ROLE=
export DATABASE_URL=sqlite:///esp-local.db
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export SCRAPE_QUEUE_BACKEND=inmemory
export WEB_SCHEDULER_MODE=disabled

python -m alembic upgrade head
flask --app app:create_cli_app create-user
flask --app app:create_cli_app run --host 127.0.0.1 --port 5000
```

設定項目の例は [.env.example](.env.example) にあります。`.env`の値が自動で読み込まれるとは限らないため、実行環境に設定してください。旧補助スクリプト `create_test_user.py` と `cleanup_db.py` は誤操作防止のため実行を停止します。

```bash
# CIと同じテスト対象。外部サイトでの実動作を保証するものではありません。
pytest tests -q
```

コードは `routes/`、`services/`、`models.py`、画面は `templates/` と `static/`、検証は `tests/` にあります。`llama.cpp/` は、このアプリの通常の変更対象に含めません。

開発にはAIコーディング支援を利用しています。開発時の指針は [AGENTS.md](AGENTS.md)、過去の説明は [旧README](docs/archive/README_20260930.md) を参照してください。

## ライセンス

公開リポジトリであることは、自由な再配布・再利用の許諾を意味しません。リポジトリ内のライセンス整理状況は [LICENSE_PENDING.md](LICENSE_PENDING.md) を確認してください。
