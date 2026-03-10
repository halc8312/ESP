# Block D-3 実装報告

- 作業日: 2026-03-10
- 対象: 公開カタログの商品詳細モーダル

---

## 1. 実施内容

### 1-1. カタログ詳細 JSON エンドポイント

- `routes/catalog.py` に `GET /catalog/<token>/product/<product_id>` を追加
- 公開中の価格表に含まれる商品のみ JSON で返すようにした
- 返却内容は以下を含む
  - `product_id`
  - `title`, `title_en`
  - `price`
  - `image_urls`
  - `stock`, `in_stock`
  - `description`, `description_en`
  - `source_url`, `site`

### 1-2. データ整形の共通化

- `catalog_view()` と詳細 JSON の両方で使う `_build_catalog_item()` を追加した
- アーカイブ済み / 削除済み商品の除外条件を一箇所に集約した
- 最新 `ProductSnapshot` から画像と説明を引く既存方針を維持した

### 1-3. 公開カタログ UI

- `templates/catalog.html` に以下を追加
  - 商品カードの `Quick View` ボタン
  - 画像ギャラリー付きモーダル本体
  - サムネイル切替
  - ソースリンク
  - ESC / 背景クリックでのクローズ
- 価格換算は既存の `data-jpy-price` ベース処理をそのまま流用し、モーダル価格も通貨切替に追従するようにした

---

## 2. 既存機能への配慮

- 公開カタログの URL と token は変更していない
- 商品一覧表示の初期データ量を無駄に増やさず、詳細だけ JSON fetch で取得する形にした
- `grid` / `editorial` の両レイアウトで同じモーダルを使うため、D-2 のレイアウト切替を壊していない
- 説明文はモーダル内で `textContent` 表示にし、公開ページ側で不用意に HTML 注入しないようにした

---

## 3. 検証

### テンプレート

- `python -c "from app import app; app.jinja_env.get_template('catalog.html'); print('D3_TEMPLATE_OK')"`
  - `D3_TEMPLATE_OK`

### 追加テスト

- `pytest tests/test_e2e_routes.py -q -k "catalog_view or catalog_product_detail or pricelist"`
  - `6 passed`

確認内容:

- 公開カタログが選択レイアウトの class で描画される
- `Quick View` とモーダル shell が HTML に入る
- 商品詳細 JSON が必要項目を返す
- 価格表に存在しない商品 ID は `404 Not found` を返す

### 回帰テスト

- `pytest tests -q`
  - `115 passed`

---

## 4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の D-3 を完了に更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に以下を反映
  - `/catalog/<token>/product/<product_id>`
  - カタログ詳細モーダルの JSON fetch 方針
  - Block D-3 完了状態

---

## 5. 次の残件

Block D の残り:

- D-4 アクセス解析
