# Block D-1 実装報告

- 作業日: 2026-03-10
- 対象: 商品手動追加機能

---

## 1. 実施内容

### 1-1. 新規ルート

- `routes/main.py` に `GET/POST /products/manual-add` を追加
- GET では手動追加フォームを表示
- POST では入力検証後に商品を作成し、商品編集ページへリダイレクトする

### 1-2. 保存ロジック

- 手動追加時に以下を同時作成するようにした
  - `Product`
  - `ProductSnapshot`
  - デフォルト `Variant`
- 画像 URL は改行または `|` 区切りで受け付け、`http(s)` と `/media/...` 形式だけ保存するようにした
- 在庫状態が `sold` の場合は `last_status='sold'`、在庫数は `0` にそろえるようにした

### 1-3. 入力項目

- 商品名（日/英）
- 商品説明（日/英）
- 仕入価格 / 販売価格
- 在庫数 / 在庫状態
- Shopify 公開状態
- サイト区分 / 元URL / タグ / SKU
- 画像 URL
- 所属ショップ

### 1-4. 所有権と安全性

- 指定された `shop_id` は必ず `current_user.id` 所有で検証するようにした
- 他ユーザーのショップを指定した場合は登録せず、同ページでエラー表示するようにした
- 元URLが入力されている場合は、同一ユーザー内の重複登録を拒否するようにした

### 1-5. 導線

- `templates/index.html` の一覧上部に `＋ 商品を手動追加` ボタンを追加
- `source_url` が空の商品は、一覧で無効リンクを出さずテキスト表示にした
- `manual` サイト区分の表示名を `手動` にした

---

## 2. 既存機能への配慮

- 既存のスクレイピング保存経路には触れていない
- 商品詳細編集は既存の `/product/<id>` をそのまま再利用している
- export / catalog が参照する `ProductSnapshot` を手動追加でも必ず生成し、後段のデータ欠落を避けた
- 一覧のソースリンク表示は `source_url` がある場合だけリンク化するようにし、既存データにも安全側に効く変更にした

---

## 3. 検証

### テンプレート

- `python -c "from app import app; [app.jinja_env.get_template(name) for name in ('index.html', 'product_manual_add.html', 'product_detail.html')]; print('D1_TEMPLATES_OK')"`
  - `D1_TEMPLATES_OK`

### 追加テスト

- `pytest tests/test_e2e_routes.py -q -k "manual_add or index_loads_when_authenticated or index_pagination"`
  - `6 passed`

確認内容:

- 未ログインでは手動追加ページへ入れない
- 手動追加ページが表示できる
- Product / ProductSnapshot / Variant が一貫した形で作成される
- 他ユーザーのショップは指定できない

### 回帰テスト

- `pytest tests -q`
  - `109 passed`

---

## 4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の D-1 を完了に更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に以下を反映
  - `product_manual_add.html`
  - `/products/manual-add`
  - 商品登録・編集フローへの手動追加経路

---

## 5. 次の残件

Block D の残り:

- D-2 カタログレイアウト切替
- D-3 商品詳細モーダル
- D-4 アクセス解析
