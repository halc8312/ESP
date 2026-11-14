# RecordCity の取得設定と診断

## 現行の取得経路

Renderのworker定義は `RECORDCITY_FETCH_PROVIDER=browser`、`RECORDCITY_BROWSER_PROFILE=persistent-chrome` を使用します。RecordCity専用のPatchright・branded Chrome・Xvfbとpersistent contextで、一覧から詳細への遷移状態を共有します。他サイトのheadless経路とは別です。

過去の単発成功は、現在の到達性や継続成功を保証しません。ブラウザ起動、Cookieの存在、HTTP応答だけで商品取得成功とせず、要求した商品との同一性、タイトル、価格、在庫を確認します。過去の比較ログ・ジョブID・費用見積りは現行の運用設定から分離し、変更履歴から追跡します。

## アクセス制限が出たとき

- Challenge、CAPTCHA、HTTPエラー、解析失敗、0件を区別する。
- CAPTCHAや429で連続再試行を増やさない。サイト側の条件・アクセス頻度・実行経路を確認する。
- Cookie・トークン・完全な応答HTMLを公開ログやPRへ貼らない。診断はUTC時刻、対象環境、Git SHA、reason code、HTTP status、WAF actionなど必要な情報に絞る。
- 売切れ・予約・削除の判断は、対象商品と一致する構造化データに基づく。アクセス失敗を売切れと扱わない。

## 診断CLI

まず対象URLと実行回数を決め、通常の取得を妨げない時間に1回ずつ確認します。次は実サイトへアクセスする操作です。

```bash
flask --app app:create_cli_app recordcity-probe 'https://www.recordcity.jp/ja/catalog/PRODUCT_ID' --strategy patchright-current --timeout-seconds 60
```

`PRODUCT_ID`を確認対象へ置き換えます。診断専用の比較経路が成功しても、その設定が本番providerへ反映された証拠にはなりません。選択肢は `recordcity-probe --help` と `services/recordcity_probe.py` を確認してください。

外部取得のAPIキー・proxyは非公開設定で管理します。キーの配置だけでproviderを自動変更しません。`--allow-external`や有料providerの有効化は課金と送信先を確認してから実施します。料金は外部サービスの現行契約で確認してください。

## 一覧取得と巡回

- 一覧先行取得は `RECORDCITY_LISTING_ENABLED` が既定で無効です。絞り込み・順序・ページ送り・重複・件数を実一覧で確認してから有効にします。
- 予約商品を含め、価格と在庫の根拠が十分かを確認します。取得率が低い結果を単に処理終了だけで成功と扱いません。
- RecordCityの登録商品巡回は実装済みです。実行結果は [監視](SCRAPE_MONITORING_RUNBOOK.md) と [巡回手順](PATROL_OPERATIONS.md) で確認します。

一覧取得の制約・上限・受け入れ条件は [TWO_STAGE_EXTRACTION](TWO_STAGE_EXTRACTION.md)、未完了項目は [DELIVERY_STATUS](DELIVERY_STATUS.md) を参照してください。
