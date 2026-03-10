# Block C-1 / C-2 実装報告

- 作業日: 2026-03-10
- 対象: 英語フィールド追加、インライン編集 API

---

## 1. 実施内容

### 1-1. C-1 英語タイトル・説明フィールド追加

- `models.py`
  - `Product.custom_title_en`
  - `Product.custom_description_en`
  を追加
- `app.py` の `run_migrations()` に以下を追加
  - `ALTER TABLE products ADD COLUMN custom_title_en VARCHAR`
  - `ALTER TABLE products ADD COLUMN custom_description_en TEXT`
- `routes/products.py` に保存処理を追加
- `templates/product_detail.html` に手動入力欄を追加

### 1-2. C-2 インライン編集 API

- `routes/api.py` に `PATCH /api/products/<id>/inline-update` を追加
- 許可フィールドを以下に限定
  - `selling_price`
  - `custom_title_en`
- 所有権チェックあり
- `selling_price` は整数かつ 0 以上でバリデーション

### 1-3. 商品一覧 UI 反映

- `templates/index.html`
  - 商品名の下に `custom_title_en` 編集欄を追加
  - 価格欄に `selling_price` 直接編集欄を追加
  - desktop/mobile 両方へ反映
  - 保存後は同一商品の desktop/mobile 入力欄を同期
- `static/css/style.css`
  - インライン編集用スタイル追加

### 1-4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の C-1 / C-2 チェックを完了へ更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に新 API / 新フィールドを反映

---

## 2. 既存機能への配慮

- 商品抽出・保存・パトロール・エクスポート処理には変更を入れていない
- 既存の `product_detail` POST パラメータは維持
- インライン編集 API は許可フィールドを限定し、既存更新ロジックと分離

---

## 3. 検証

### 構文確認

- `python -c "from app import app; [app.jinja_env.get_template(name) for name in ('index.html', 'product_detail.html')]; print('INDEX_TEMPLATES_OK')"`
  - `INDEX_TEMPLATES_OK`

### テスト

- `pytest tests/test_e2e_routes.py -q`
  - `49 passed`
- `pytest tests -q`
  - `96 passed`

### 追加した確認観点

- `product_detail` から `custom_title_en` / `custom_description_en` が保存されること
- `PATCH /api/products/<id>/inline-update` が `custom_title_en` を更新できること
- 同 API が `selling_price` を更新できること
- 未許可フィールドは `400` になること
- 他ユーザー所有商品の更新は `404` になること

---

## 4. 次の残件

Block C の残り:

- C-3 一括価格設定 API
- C-4 抽出結果の同画面表示 + 選択登録
- C-5 画像削除・並べ替え

現時点では C-3 に進むのが順当。
