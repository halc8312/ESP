# Block C-3 実装報告

- 作業日: 2026-03-10
- 対象: 一括価格設定 API

---

## 1. 実施内容

### 1-1. API 追加

- `routes/api.py` に `POST /api/products/bulk-price` を追加
- 対応モード:
  - `margin`
  - `fixed_add`
  - `fixed`
  - `margin_plus_fixed`
  - `reset`

### 1-2. バリデーション

- `ids` は整数配列のみ許可
- `margin` 系は `0 <= margin < 100` を強制
- `fixed` は 0 以上の整数を要求
- 所有権チェックあり
- 仕入価格 (`last_price`) がない商品は、価格原価が必要なモードでスキップ

### 1-3. 商品一覧 UI 統合

- `templates/index.html` の一括操作セレクトに `販売価格を一括設定` を追加
- 既存の一括操作ボタンから prompt ベースで呼び出し
- API 応答後、対象商品の販売価格表示とインライン入力欄を即時同期

### 1-4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の C-3 チェックを完了へ更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に `POST /api/products/bulk-price` を追記

---

## 2. 計算方針

### margin

- `selling_price = cost / (1 - margin/100)`
- ロードマップ記載の利益率定義に合わせた

### fixed_add

- `selling_price = cost + add_value`

### fixed

- `selling_price = fixed_value`

### margin_plus_fixed

- `selling_price = cost / (1 - margin/100) + fixed_value`

### reset

- `selling_price = NULL`

---

## 3. 既存機能への配慮

- 既存の価格ルール (`pricing_rule_id`) ロジックには手を入れていない
- 一括価格設定は明示 API として追加し、既存のエクスポートや商品詳細更新とは分離
- 商品抽出・スクレイパー・パトロールには影響なし

---

## 4. 検証

### 構文確認

- `python -c "from app import app; app.jinja_env.get_template('index.html'); print('INDEX_OK')"`
  - `INDEX_OK`

### テスト

- `pytest tests/test_e2e_routes.py -q`
  - `52 passed`

追加した確認:

- `margin` モードで販売価格が再計算されること
- `reset` モードで `selling_price` が `NULL` に戻ること
- 不正な `margin=100` を `400` で拒否すること

---

## 5. 次の残件

Block C の残り:

- C-4 抽出結果の同画面表示 + 選択登録
- C-5 画像削除・並べ替え

次は C-4 に進むのが順当。
