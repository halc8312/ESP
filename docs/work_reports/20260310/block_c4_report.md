# Block C-4 実装報告

- 作業日: 2026-03-10
- 対象: 抽出結果の同画面表示 + 選択登録

---

## 1. 実施内容

### 1-1. プレビュー用抽出経路

- `routes/scrape.py` の `_build_scrape_task()` に `persist_to_db` 引数を追加
- preview モードでは抽出後に DB 保存せず、`items` / `excluded_count` をキュー結果に保持
- `POST /scrape/run` に `response_mode=preview` を追加し、JSONで `job_id` と `status_url` を返すようにした

### 1-2. 選択登録 API

- `POST /scrape/register-selected` を追加
- キューに保持された結果から `selected_indices` 分だけ取り出して保存
- `save_scraped_items_to_db()` をそのまま使うため、既存保存ロジックとは二重実装にしていない

### 1-3. 所有者チェック

- `services/scrape_queue.py` の `get_status()` に `user_id` 指定を追加
- `routes/api.py` の `/api/scrape/status/<job_id>` でも所有者チェックを有効化
- 他ユーザーのジョブは `404 Job not found` にした

### 1-4. フロントエンド

- `templates/scrape_form.html`
  - フォーム送信を JS で preview モードへ切り替え
  - 同画面ポーリングで抽出結果を表示
  - サムネイル付きカード + チェックボックスで選択
  - `選択した商品を登録` ボタンから `register-selected` を呼び出し
- `static/css/style.css`
  - プレビューカード、結果ツールバー、通知帯のスタイルを追加

### 1-5. 後方互換

- JS が使えない場合は従来どおり通常送信
- 通常送信では既存の待機ページ → 結果ページ → 即時保存フローを維持

---

## 2. 既存機能への配慮

- スクレイパー本体には変更を入れていない
- 保存ロジックは `save_scraped_items_to_db()` を再利用
- 既存の待機ページと結果ページは残した
- preview モードだけ新経路、従来モードは維持という形にした

---

## 3. 検証

### テンプレート

- `python -c "from app import app; [app.jinja_env.get_template(name) for name in ('scrape_form.html', 'scrape_waiting.html', 'scrape_result.html')]; print('SCRAPE_TEMPLATES_OK')"`
  - `SCRAPE_TEMPLATES_OK`

### 追加テスト

- `pytest tests/test_scrape_preview_flow.py -q`
  - `4 passed`

確認内容:

- preview モードでは DB に即時保存されない
- 通常送信では従来どおり redirect される
- `register-selected` が選択商品だけ保存する
- 他ユーザーのジョブはステータス取得できない

### 回帰テスト

- `pytest tests -q`
  - `103 passed`

---

## 4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の C-4 を完了に更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に以下を反映
  - `/scrape/register-selected`
  - JS preview 経路 + 非 JS 従来経路の併存

---

## 5. 次の残件

Block C の残り:

- C-5 画像削除・並べ替え

その後は Block D に進める状態。
