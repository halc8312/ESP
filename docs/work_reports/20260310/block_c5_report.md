# Block C-5 実装報告

- 作業日: 2026-03-10
- 対象: 商品画像の削除・並べ替え・追加

---

## 1. 実施内容

### 1-1. 画像更新の保存方式

- `routes/products.py` に画像 URL 更新用の補助関数を追加
- 商品編集 POST 時に `image_urls_json` を受け取り、順序変更・削除・追加を判定するようにした
- 画像リストが変わった場合だけ、新しい `ProductSnapshot` を 1 件追加して最新状態を差し替えるようにした

### 1-2. 既存データ保全

- 既存の `ProductSnapshot` は上書きしない
- export / catalog / 一覧で参照している `latest snapshot` の前提は維持した
- 画像編集だけで `Product` 本体やスクレイピング保存処理の責務を崩さない構成にした

### 1-3. 商品編集 UI

- `templates/product_detail.html` に以下を追加
  - SortableJS によるドラッグ並べ替え
  - 画像削除ボタン
  - `image_urls_json` 隠しフィールド
  - 手動 URL 追加フォーム
- アップロードはまだ入れず、ロードマップどおり後回しにした

### 1-4. スタイル調整

- `static/css/style.css` に画像カード、並べ替えハンドル、空状態、URL 追加フォームのスタイルを追加
- 既存の 2 カラム商品編集レイアウトは維持したまま差し込んだ

---

## 2. 既存機能への配慮

- スクレイパー本体には変更を入れていない
- `ProductSnapshot.image_urls` を参照する export / catalog の経路はそのまま維持した
- 画像が変わらない保存では新規 snapshot を増やさないようにした
- 受信した画像 URL は `http(s)` と `/` から始まるものだけ受け付け、重複は除外するようにした

---

## 3. 検証

### テンプレート

- `python -c "from app import app; [app.jinja_env.get_template(name) for name in ('product_detail.html', 'index.html', 'scrape_form.html')]; print('TEMPLATES_OK')"`
  - `TEMPLATES_OK`

### 追加テスト

- `pytest tests/test_e2e_routes.py -q -k "product_detail_update or product_detail_loads or product_detail_403 or product_detail_requires_login"`
  - `6 passed`

確認内容:

- 商品詳細画面の既存表示・権限制御が維持される
- 画像の削除・並べ替えで新しい snapshot が追加される
- snapshot 未作成商品でも URL 追加から画像を持てる

### 回帰テスト

- `pytest tests -q`
  - `105 passed`

---

## 4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の C-5 を完了に更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に以下を反映
  - 商品編集フロー
  - `image_urls_json` による画像順序送信
  - `ProductSnapshot` を使った最新画像管理

---

## 5. 次の残件

Block C は完了。
次は Block D に進む段階。
