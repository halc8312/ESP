# Block D-2 実装報告

- 作業日: 2026-03-10
- 対象: カタログレイアウト切替

---

## 1. 実施内容

### 1-1. PriceList.layout 追加

- `models.py` に `PriceList.layout` を追加
- 値は `grid` / `editorial` の 2 種類とした
- `app.py` の軽量 migration に `price_lists.layout` の追加処理を入れた

### 1-2. 価格表管理 UI

- `routes/pricelist.py` で `layout` を正規化して保存するようにした
- `templates/pricelist_edit.html` にレイアウト選択 UI を追加した
- `templates/pricelist_list.html` に現在のレイアウト表示を追加した

### 1-3. 公開カタログ

- `templates/catalog.html` を `PriceList.layout` で分岐するようにした
- `grid` は既存のカードグリッドを維持
- `editorial` は画像を大きく見せる縦積みレイアウトにした
- 既存の価格換算 JS と公開 URL はそのまま維持した

---

## 2. 既存機能への配慮

- 既存価格表は `layout` 未設定でも `grid` 扱いになるようにした
- カタログ URL 構造や token は変更していない
- 商品データ構造、価格表アイテム構造、為替換算処理には互換を保った
- `pricelist_edit.html` に `currency_rate` hidden を入れ、編集時に既存値を不用意に落とさないようにした

---

## 3. 検証

### テンプレート

- `python -c "from app import app; [app.jinja_env.get_template(name) for name in ('pricelist_edit.html', 'pricelist_list.html', 'catalog.html')]; print('D2_TEMPLATES_OK')"`
  - `D2_TEMPLATES_OK`

### 追加テスト

- `pytest tests/test_e2e_routes.py -q -k "pricelist or catalog_view_uses_pricelist_layout"`
  - `3 passed`

確認内容:

- 価格表作成時に layout が保存される
- 価格表編集で layout を切り替えられる
- 公開カタログが指定 layout の class で描画される

### 回帰テスト

- `pytest tests -q`
  - `112 passed`

---

## 4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の D-2 を完了に更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に以下を反映
  - `PriceList.layout`
  - `grid` / `editorial` 切替
  - Block D-2 完了状態

---

## 5. 次の残件

Block D の残り:

- D-3 商品詳細モーダル
- D-4 アクセス解析
