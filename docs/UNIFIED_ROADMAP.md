# ESP 統合開発ロードマップ

> **このドキュメントは以下の 2 つの計画を統合したものです：**
> - `docs/specs/` — Playwright 移行計画（Stage 0〜4）
> - `reports.md` — UI/機能改善計画（Phase 1〜3）
>
> 2 つの計画を別々に進めると、同一ファイルへの変更がコンフリクトする恐れがあるため、
> 本ドキュメントで優先度・依存関係・コンフリクトリスクを一元管理します。

---

## 現在の実装状態（2026-03-09 時点）

### Playwright 移行ステータス

| Stage | 名称                              | 状態     | 完了日       |
|-------|-----------------------------------|----------|--------------|
| 0     | キューシステム構築                | ✅ 完了  | 2026-03-06   |
| 1     | ラクマ Playwright 移行            | ✅ 完了  | 2026-03-06   |
| 2     | メルカリパトロール Playwright 移行 | ✅ 完了  | 2026-03-07   |
| 3     | メルカリ全体 Playwright 移行      | ✅ 完了  | 2026-03-09   |
| 4a    | Selenium 削除（パトロール層）     | ✅ 完了  | 2026-03-09   |
| 4b    | Selenium 削除（DB 層）            | 🔲 未着手 | —            |

**Stage 4a 完了内容（本PR）:**
- `services/patrol/yahoo_patrol.py` — `_fetch_with_selenium()` 削除
- `services/patrol/offmall_patrol.py` — `_fetch_with_selenium()` 削除
- `services/patrol/snkrdunk_patrol.py` — `_fetch_with_selenium()` 削除
- `services/patrol/yahuoku_patrol.py` — `_fetch_with_selenium()` 削除
- `services/patrol/surugaya_patrol.py` — `_fetch_with_selenium()` 削除
- `surugaya_db.py` — `_fetch_soup_with_selenium()` を StealthyFetcher 実装に置換
- `debug_scrape.py`, `debug_children.py`, `debug_variant_json.py` — 削除

**Stage 4b（残タスク）:**
以下のファイルはまだ Selenium を使用しており、個別の移行が必要：
- `yahoo_db.py` — `create_driver()`, Yahoo! ショッピング商品取得
- `yahuoku_db.py` — ヤフオク商品取得
- `snkrdunk_db.py` — SNKRDUNK 商品取得
- `offmall_db.py` — オフモール商品取得
- `requirements.txt` — `selenium`, `webdriver-manager`, `undetected-chromedriver`（4b 完了後に削除）
- `Dockerfile` — Google Chrome インストールブロック（4b 完了後に削除）

### UI/機能改善ステータス（reports.md より）

#### Phase 1: 商品一覧ページ（`/`）

| # | 機能 | 状態 | 備考 |
|---|------|------|------|
| 1-1 | 検索項目コンパクト化 | ✅ 完了 | `<details>` + 詳細フィルタ実装済み |
| 1-2 | eBay 関連項目削除 | ✅ 完了 | テンプレートから削除済み |
| 1-3 | 「サイト」列削除 | ✅ 完了 | テーブルから削除済み |
| 1-4 | 「画像枚数」列削除 | ✅ 完了 | テーブルから削除済み |
| 1-5 | 「ステータス」→「在庫」 | ✅ 完了 | stock-badge 実装済み |
| 1-6 | 「元URL」→「抽出サイト」 | ✅ 完了 | site_display マッピング実装済み |
| 1-7 | 価格列二段表示 | ✅ 完了 | 仕入/販売 二段表示実装済み |
| 1-8 | 商品名英語フィールド表示 | 🔲 未着手 | DB マイグレーション（`custom_title_en`）が必要 |
| 1-9 | インライン価格・英語名編集 | 🔲 未着手 | 1-8 完了後に着手 |
| 1-10 | 一括価格設定 | 🔲 未着手 | |
| 1-11 | 商品手動追加 | 🔲 未着手 | |

#### Phase 2: 商品編集ページ（`/product/<id>`）

| # | 機能 | 状態 | 備考 |
|---|------|------|------|
| 2-1 | 商品編集コンパクト化 | 🔲 未着手 | |
| 2-2 | 日本語/英語フィールド追加 | 🔲 未着手 | 1-8 と同時着手推奨 |
| 2-3a | 画像削除・並べ替え | 🔲 未着手 | SortableJS 導入 |
| 2-3b | 画像アップロード | 🔲 未着手 | ストレージ選択が必要 |
| 2-3c | 画像白抜き | 🔲 未着手 | remove.bg API 等 |

#### Phase 3: 商品抽出ページ（`/scrape/`）

| # | 機能 | 状態 | 備考 |
|---|------|------|------|
| 3-1 | 「スクレイピング」→「商品抽出」 | ✅ 完了 | テンプレート全体で変更済み |
| 3-2 | ローディング画面 | ✅ 完了 | JS オーバーレイ実装済み |
| 3-3 | 検索画面コンパクト化 | ✅ 完了 | コンパクトレイアウト実装済み |
| 3-4 | 同画面サムネイル結果表示 | 🔲 未着手 | Ajax/ポーリング実装が必要 |
| 3-5 | チェックボックス選択結果表示 | 🔲 未着手 | 3-4 完了後 |
| 3-6 | 選択商品のみ登録 | 🔲 未着手 | 3-5 完了後 |

#### Phase 4: 価格表管理ページ（`/pricelists`）

| # | 機能 | 状態 | 備考 |
|---|------|------|------|
| 4-1 | カタログレイアウト切替 | 🔲 未着手 | |
| 4-2 | 商品詳細モーダル | 🔲 未着手 | Ajax API エンドポイントが必要 |
| 4-3 | アクセス解析 | 🔲 未着手 | `CatalogPageView` モデルが必要 |

---

## 統合ロードマップ（推奨実施順序）

### Sprint A: 技術的負債解消（Stage 4b）
**目標**: Selenium を完全削除し、Docker イメージを軽量化する

1. `yahoo_db.py` を Scrapling StealthyFetcher に移行
2. `yahuoku_db.py` を Scrapling StealthyFetcher に移行
3. `snkrdunk_db.py` を Scrapling StealthyFetcher に移行
4. `offmall_db.py` を Scrapling StealthyFetcher に移行
5. `requirements.txt` から `selenium`, `webdriver-manager`, `undetected-chromedriver` を削除
6. `Dockerfile` から Google Chrome インストールブロックを削除
7. `docs/specs/MIGRATION_COMPLETE.md` を作成

**コンフリクトリスク**: なし（UI 変更と独立）
**所要時間目安**: 2〜3日

---

### Sprint B: 商品情報の英語化基盤（reports.md 1-8, 2-2）
**目標**: 商品の英語名・英語説明文フィールドを追加する

1. `models.py` に `custom_title_en` カラムを追加
2. `app.py` の `run_migrations()` に `ALTER TABLE` を追加
3. `templates/index.html` で英語名を二段表示
4. `templates/product_detail.html` に英語フィールドを追加
5. `routes/main.py` の保存処理に英語フィールドを含める

**コンフリクトリスク**: Sprint A と並行実施可能（対象ファイルが異なる）
**所要時間目安**: 1日

---

### Sprint C: インライン編集・一括操作（reports.md 1-9, 1-10）
**目標**: 商品一覧からダイレクトに価格・英語名を編集できるようにする

1. `/api/product/<id>/quick-edit` API エンドポイントを追加
2. `templates/index.html` にインライン編集 UI を追加（JS）
3. 一括価格設定モーダルを追加

**コンフリクトリスク**: Sprint B 完了後に実施（英語名フィールドが前提）
**所要時間目安**: 1〜2日

---

### Sprint D: 商品抽出 UX 改善（reports.md 3-4〜3-6）
**目標**: 抽出結果を同画面で確認・選択登録できるようにする

1. 抽出結果をポーリング取得する JS を実装
2. `routes/scrape.py` に部分結果返却 API を追加
3. サムネイル付き候補一覧表示 UI を追加
4. チェックボックス + 一括登録ボタンを実装

**コンフリクトリスク**: Sprint A の `routes/scrape.py` 変更と被る可能性あり → Sprint A 完了後に着手
**所要時間目安**: 2〜3日

---

### Sprint E: カタログ機能強化（reports.md 4-2, 4-3）
**目標**: 価格表カタログページを強化する

1. `CatalogPageView` モデルを `models.py` に追加
2. `run_migrations()` に対応する `ALTER TABLE` / `CREATE TABLE` を追加
3. 商品詳細モーダル API (`/catalog/<token>/product/<id>`) を実装
4. アクセス解析ダッシュボードページを実装

**コンフリクトリスク**: Sprint B の `models.py` 変更と重複する可能性あり → Sprint B 完了後に着手
**所要時間目安**: 2日

---

## コンフリクト防止ガイドライン

### ファイル担当マトリクス

| ファイル / スプリント | A (Selenium削除) | B (英語化) | C (インライン編集) | D (抽出UX) | E (カタログ) |
|----------------------|:---:|:---:|:---:|:---:|:---:|
| `models.py` | — | ✏️ | — | — | ✏️ |
| `app.py` (migrations) | — | ✏️ | — | — | ✏️ |
| `templates/index.html` | — | ✏️ | ✏️ | — | — |
| `templates/product_detail.html` | — | ✏️ | — | — | — |
| `templates/scrape_form.html` | — | — | — | ✏️ | — |
| `routes/scrape.py` | — | — | — | ✏️ | — |
| `routes/catalog.py` | — | — | — | — | ✏️ |
| `yahoo_db.py` etc. | ✏️ | — | — | — | — |
| `requirements.txt` | ✏️ | — | — | — | — |
| `Dockerfile` | ✏️ | — | — | — | — |

**✏️ = 変更あり。同一スプリント内での変更はアトミックに実施すること。**

### 並行実施可能な組み合わせ

- **A + B**: 完全に独立したファイルを変更するため、並行実施可能
- **C は B 完了後**: `custom_title_en` フィールドが前提
- **D は A 完了後推奨**: `routes/scrape.py` の変更が A で起きる可能性がある（現状はなし）
- **E は B 完了後推奨**: `models.py` の変更が重複する可能性がある

---

## 参照ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/specs/STAGE_0_QUEUE_SYSTEM.md](./specs/STAGE_0_QUEUE_SYSTEM.md) | キューシステム仕様 |
| [docs/specs/STAGE_1_RESULTS.md](./specs/STAGE_1_RESULTS.md) | Stage 1 ラクマ移行結果 |
| [docs/specs/STAGE_3_RESULTS.md](./specs/STAGE_3_RESULTS.md) | Stage 3 メルカリ移行結果 |
| [docs/specs/STAGE_4_SELENIUM_REMOVAL.md](./specs/STAGE_4_SELENIUM_REMOVAL.md) | Stage 4 Selenium 削除仕様 |
| [reports.md](../reports.md) | UI/機能改善の詳細提案 |
