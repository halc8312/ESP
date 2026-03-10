# 資料同期と Block B 完了報告

- 作業日: 2026-03-10
- 対象範囲: Stage 4b 完了の資料反映、商品抽出文言の統一、Block B-8 実装

---

## 1. 実施内容

### 1-1. 資料同期

- `docs/specs/STAGE_4_RESULTS.md` を新規作成
- `docs/specs/README.md` の Stage 4b 状態を `完了` へ更新
- `docs/specs/CURRENT_ARCHITECTURE.md` を現行コード基準で再整理
- `docs/UNIFIED_ROADMAP.md` の Stage 4b 状態、Block A チェック、Block B チェックを更新

### 1-2. 商品抽出まわりの整理

- `routes/main.py` から `index.html` 未使用の `default_ebay_*` コンテキストを削除
- `routes/scrape.py` のエラーメッセージを `商品抽出` 表記へ統一
- `templates/scrape_waiting.html` の表示文言を `商品を抽出しています...` に変更
- `templates/scrape_result.html` のヘッダーとサマリー文言を `商品抽出結果` / `抽出件数` へ変更
- `templates/base.html` のモバイル下部ナビ文言を `抽出` へ変更

### 1-3. B-8 商品編集ページ コンパクト化

- `templates/product_detail.html` を再構成
  - PC: メイン列 + サイド列の 2 カラム
  - モバイル: `<details>` ベースのアコーディオン表示
  - SEO セクション: デフォルト折りたたみ
  - 画像セクション: サイドカラムへ移動
- `static/css/style.css` に商品編集ページ専用のレイアウト/CSSを追加

---

## 2. 互換性上の配慮

- POST フィールド名は変更していない
- バリエーション追加/削除の JavaScript 関数名は維持
- 既存の TinyMCE 初期化対象 `#description` は維持
- スクレイピング実処理や保存ロジックには手を入れていない

---

## 3. 検証

### 構文・テンプレート

- `python -c "from app import app; [app.jinja_env.get_template(name) for name in ('base.html', 'scrape_waiting.html', 'scrape_result.html', 'product_detail.html')]; print('TEMPLATES_OK')"`
  - `TEMPLATES_OK`

### テスト

- `pytest tests -q`
  - `92 passed`

### 目視用確認項目

- `default_ebay` は `routes/` と `templates/` から除去済み
- `スクレイピング中` / `取得結果` の現行 UI 向け残骸は解消済み
  - 旧仕様書 (`docs/specs/STAGE_0_QUEUE_SYSTEM.md`) の履歴記述は意図的に残置

---

## 4. 現時点の残件

今回で Block B は完了扱いに更新した。
残件は主に以下。

- Block C-1: 英語タイトル・説明カラム追加
- Block C-2: インライン編集 API
- Block C-3: 一括価格設定 API
- Block C-4: 抽出結果の同画面表示 + 選択登録
- Block C-5: 画像削除・並べ替え
- Block D 全体
- 実サイト向け自動スモークテスト整備
