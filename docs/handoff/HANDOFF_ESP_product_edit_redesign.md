# ESP 商品編集リデザイン 引き継ぎドキュメント (PR3 / PR4 / PR5)

**作成日**: 2026-04-28
**作成元**: Devin (session `984538393ca24e0e96a5e35a74dc5982`)
**対象**: 別 AI ツール (Claude Code / Codex / Cursor 等) で続きを実装する担当
**リポジトリ**: https://github.com/halc8312/ESP
**ローカル clone パス**: `/home/ubuntu/repos/ESP/` (devin VM 内、引き継ぎ先ではあなたの環境で fresh clone 推奨)
**メイン branch**: `main` (→ origin/main = `0ac9268`)

---

## 0. なぜ引き継ぐか / 進捗概要

ユーザー (`halc8312`) からのリデザイン要求を、リスクを下げるため **5 本の段階的 PR に分割**して進めています。**5 本中 2 本完了 (PR1, PR2)、3 本未着手 (PR3, PR4, PR5)** の状態で別 AI に引き継ぎます。

### 完了済 PR

| PR | 状態 | branch | 内容 |
|---|---|---|---|
| #96 | **マージ済** (`e81083b`) | `devin/1777324280-non-paypal-ui` | カタログ list 高密度行 + タグ pill + 画像ライトボックス + 個別価格オーバーライド |
| #97 (PR1) | **マージ済** (`e25087e`) | `devin/1777349400-product-edit-refactor-pr1` | 商品編集の `<details>` 廃止 → flat `<section>`、sticky header、仕入設定カード新設 |
| #98 | **マージ済** | `devin/update-skills-1777365542` | `.agents/skills/testing-product-edit/SKILL.md` 追加 (回帰テスト手順) |
| **#99 (PR2)** | **OPEN・E2E テスト済・全 PASS** | `devin/1777365876-product-edit-image-pr2` | 商品画像 10 列グリッド + hover 円形アクション + Sortable + bg-removal preview 移植 |

PR #99 は CI green / E2E 全 24 アサーション PASS 済。**ユーザーがマージするのを待っている状態**。次に着手するのは **PR3 (販売設定独立化)**。

### 残り PR (本ドキュメントの主目的)

| PR | branch 推奨名 | 内容 | リスク |
|---|---|---|---|
| **PR3** | `devin/<ts>-product-edit-sales-pr3` | 販売設定 section 独立 + variant table を disclosure 化 + 単一バリアント section 直下フォールバック | **高** (CSV エクスポート互換が壊れやすい) |
| **PR4** | `devin/<ts>-product-edit-seo-pr4` | SEO カード独立 + 英語タイトル → URL ハンドル/SEO タイトル 自動同期 JS | 低 |
| **PR5** | `devin/<ts>-product-edit-sidebar-pr5` | 右サイドカラム新設 + `Product.category` カラム追加 + 公開カタログ category フィルタ | **高** (DB マイグレ + 公開カタログ連動) |

---

## 1. ファイルレイアウト (重要)

```
/home/ubuntu/repos/ESP/
├── AGENTS.md                      ← 高リスク不変条件・推奨検証コマンド
├── README.md
├── app.py                         ← Flask app factory
├── models.py                      ← SQLAlchemy モデル
├── database.py                    ← ADDITIVE_STARTUP_MIGRATIONS あり
├── cli.py                         ← flask cli カマンド
├── routes/
│   ├── products.py                ← 商品編集 POST /product/<id>     【PR3 で触る】
│   ├── api.py                     ← /api/products/<id>/recalc-price  【PR3 で触る】
│   ├── catalog.py                 ← 公開カタログ                     【PR5 で触る】
│   ├── export.py                  ← eBay/Shopify CSV 出力             【PR3 で互換性確認】
│   └── ...
├── services/
│   ├── pricing_service.py         ← calculate_selling_price (manual override 反映済)
│   ├── product_service.py
│   └── monitor_service.py
├── templates/
│   ├── product_detail.html        ← 商品編集メイン        【PR3/PR4/PR5 全部で触る】 (現状 2802 行)
│   ├── product_manual_add.html    ← 手動追加 (2 カラム)   【PR3/PR5 で壊さない】
│   ├── catalog.html               ← 公開カタログ           【PR5 で触る】
│   └── ...
├── static/
│   ├── css/style.css              ← メイン CSS (5349 行)   【全 PR で触る】
│   ├── css/catalog.css
│   └── js/product_bg_removal.js   ← bg removal クライアント
├── tests/
│   ├── test_e2e_routes.py         ← 95 件のメイン E2E    【全 PR で green を保つ】
│   ├── test_worker_runtime.py
│   └── ...
└── .agents/skills/
    ├── testing-product-edit/SKILL.md     ← PR1/2 の E2E 手順 (拡充して再利用可能)
    └── render-deployment/
```

### モックアップ (デザインの正)
- 商品編集モック: `/home/ubuntu/attachments/c6b46db6-937c-4b15-aca2-15bd6f76db82/.html` (PSA10 ワンピースカード)
  - もしくは `/home/ubuntu/attachments/4b81a64d-91c0-4c94-855b-8d08547bc25c/.html` (どちらも商品編集モック)
- カタログ A モック: `/home/ubuntu/attachments/zip_extracted/260322修正/カタログA.html` (grid)
- カタログ B モック: `/home/ubuntu/attachments/zip_extracted/260322修正/カタログB.html` (high-density list)
- 商品編集モック (古): `/home/ubuntu/attachments/zip_extracted/260322修正/商品編集.html`

引き継ぎ先 AI が VM 内のこれらにアクセスできない場合は、**ユーザーに再添付を依頼**してください (ユーザーは halc8312 / tatsuki.n.0605@gmail.com)。

### Devin が作った参考ドキュメント (読むと早い)
- `/home/ubuntu/260322_gap_analysis.md` … 初回ギャップ分析
- `/home/ubuntu/260427_product_edit_gap_analysis.md` … 商品編集モック vs 現状の詳細ギャップ分析 ★必読
- `/home/ubuntu/260427_must_keep_list.md` … 残すべき機能リスト ★必読
- `/home/ubuntu/test_plan_pr96.md` … PR #96 用テスト計画
- `/home/ubuntu/test_report_pr96.md` … PR #96 用テスト結果
- `/home/ubuntu/test_plan_pr97.md` … PR #97 (PR1) 用テスト計画
- `/home/ubuntu/test_report_pr97.md` … PR #97 (PR1) 用テスト結果
- `/home/ubuntu/test_plan_pr99.md` … PR #99 (PR2) 用テスト計画 (テンプレとして再利用可)
- `/home/ubuntu/test_report_pr99.md` … PR #99 (PR2) 用テスト結果

---

## 2. 高リスク不変条件 (絶対に壊さない)

### 2.1 AGENTS.md より
1. **公開カタログに `source_url` / `site` / 内部仕入情報を絶対に出さない** (PR5 で公開カタログを触るときに最重要)
2. **user/shop/pricelist isolation を保つ** (商品は `user_id` + `shop_id` でスコープ)
3. **render.yaml の web/worker/db/queue 契約を変えない**
4. **本番 SECRET_KEY は esp-web と esp-worker 間で共有されること**

### 2.2 既存データ・契約の互換 (Devin が確立した約束)
1. **マルチバリアント table を残す**
   - `models.Variant` (table `variants`) は eBay/Shopify CSV エクスポート (`routes/export.py`) で必須
   - **モックは単一バリアント前提だが、既存 DB との互換のため "ハイブリッド UX" を実装する** (詳細は PR3 セクションへ)
2. **既存 form の `name=` 属性を変えない**
   - `routes/products.py` を変更しないため
   - `name="title"`, `name="title_en"`, `name="custom_description"`, `name="custom_description_en"`,
     `name="tags"`, `name="vendor"`, `name="handle"`, `name="seo_title"`, `name="seo_description"`,
     `name="status"`, `name="shop_id"`, `name="pricing_rule_id"`,
     `name="manual_price_enabled"`, `name="manual_margin_rate"`, `name="manual_shipping_cost"`,
     `name="option1_name"`, `name="option2_name"`, `name="option3_name"`,
     `name="v_opt1_<i>"`, `name="v_opt2_<i>"`, `name="v_price_<i>"`, `name="v_sku_<i>"`, `name="v_qty_<i>"`,
     `name="v_grams_<i>"`, `name="v_tax_<i>"`, `name="v_hs_<i>"`, `name="v_org_<i>"`,
     `name="image_files"`, `name="image_urls_json"` (hidden) 等
3. **`tags` はカンマ区切り文字列で DB 保存** (pill UI は hidden input にミラーする)
4. **画像 URL + ファイルアップロード両対応**
   - `imageUrls` JS 配列 → `image_urls_json` hidden → server で `_parse_image_urls_json()` でパース
   - 画像実体は `product_snapshots.image_urls` に `|` 区切りで保存される (注: products テーブルには image_urls カラムなし)
5. **CSRF / Flask-WTF を必ず付ける**
   - Ajax 系の fetch には `<meta name="csrf-token">` から取って `X-CSRFToken` ヘッダで送る (`aa26325` で修正したパターン)
6. **公開ステータス**: 現在 `status` カラムが `VARCHAR` で `'active'` / `'draft'` の運用
   - `is_published` boolean ではなく **`status` 列がすでに存在する** (PRAGMA で確認済)
   - PR5 で `archived` boolean → status の値統合する場合のみマイグレ要

### 2.3 やってはいけないこと
- `git push --force` を main/master へ
- pre-commit hook を `--no-verify` でスキップ
- commit を `--amend` する
- `routes/products.py` の form フィールド受け取りロジックを変える (PR3 で manual override の保存だけは触ってよいが、最小限で)
- generated files (例えば `migrations/versions/`) を手で書き換える → 既存パターンの `ADDITIVE_STARTUP_MIGRATIONS` を使う

---

## 3. PR3: 販売設定 independence + variant table disclosure + single-variant fallback

### 3.1 目的
モックは「販売価格」「在庫数」を section 直下に 1 件だけ並べる単一バリアント前提だが、ESP は複数バリアント前提。両立する **ハイブリッド UX** を実装する。

### 3.2 仕様 (UX 詳細)

#### 3.2.1 `variants|length == 1` の場合 (大半のユーザー)
- section 直下に **販売価格 (¥)** と **在庫数** の 2 input を直接表示
- 入力値は内部で `v_price_1` / `v_qty_1` の hidden field にミラーする
- **「詳細バリエーション設定」** disclosure (`<details>`) はデフォルト閉じ
  - 開くと SKU / 重量 / 税 / HS code / 原産国 / option1〜3 などフルテーブルが出る
  - SKU 等を編集したい上級ユーザー向け
- **「+ バリエーションを追加する」** ボタンを押すと variants が 2 件以上になり、即座に従来の table 表示に切替

#### 3.2.2 `variants|length >= 2` の場合
- section 直下フォールバックは出さない
- 従来通りの table を**常時表示** (disclosure 不要)
- モック寄せの cosmetic 改善のみ (ヘッダー余白・色調整)

#### 3.2.3 manual override (個別の利益率/送料) の再配置
- 現状: バリエーション設定 section 内、option name の上にあるトグル
- 移動先: **販売設定 section の最下部** (variant disclosure とは独立)
  - トグル `[ ] 個別の利益率・送料を有効にする`
  - その下に「個別の利益率 (%)」「個別の追加送料 (¥)」「再計算して価格に反映」ボタン
- 既存 fetch (`POST /api/products/<id>/recalc-price`) はそのまま使う

#### 3.2.4 PricingRule 選択 (`pricing_rule_id`)
- 現状: 基本設定または別 section に埋め込まれている (要確認、`templates/product_detail.html` で grep)
- 移動先: 販売設定 section の上部 (manual override より前)
- 「適用ルール: ___」select。ルール無し or 「(共通ルールに従う)」option を残す

### 3.3 触るファイル
1. `templates/product_detail.html`
   - 「商品画像」section の直後に新セクション `<section class="product-edit-section product-sales">` を挿入
   - 既存の「バリエーション設定」section から price/qty/manual override を切り出して上記の場所に再配置
   - variants table は disclosure 化 (`<details><summary>詳細バリエーション設定</summary>...</details>`)
   - 単一バリアント時の section 直下 input を Jinja で出し分け (`{% if variants|length == 1 %}` または JS で hide/show)
2. `static/css/style.css`
   - 新セクション用クラスのスタイル追加
   - 単一バリアント inline form のレイアウト
   - disclosure の summary スタイル
3. `routes/products.py`
   - **理想: 触らない** (form name 互換のため)
   - もし `v_price_*` / `v_qty_*` の構造を変えるなら最小限で
4. `routes/export.py`
   - **絶対に動作確認** (eBay / Shopify CSV エクスポート)
   - PR3 の前後でテストデータをエクスポートし diff を取る
5. `tests/test_e2e_routes.py`
   - 既存テストが壊れていないか必須確認
   - 新たに「単一バリアント時に section 直下 input が編集できる」テストを 1 つ追加するのが望ましい

### 3.4 実装の流れ (推奨)
1. branch 切り `git checkout -b devin/<ts>-product-edit-sales-pr3 main` (PR2 マージ後)
2. variants の Jinja ループ前に `{% set vlen = variants|length %}` を置く
3. section の HTML 雛形を「商品画像」section の直後に挿入
4. JS は最小限。`+ バリエーション追加` を押したら section 直下 input を hide、disclosure を open する切替を追加
5. CSS で section 直下フォームをモック準拠の 2 列レイアウトに
6. 手動テスト: variants 1 件 / 2 件で UI が切り替わること、保存後に DB に反映されること
7. CSV エクスポート diff (`flask` CLI または UI で `/products/export.csv` 等を呼ぶ — `routes/export.py` 参照)
8. `pytest tests/test_e2e_routes.py -q` 全 pass を確認
9. PR 作成

### 3.5 落とし穴 / 注意点
- **`v_price_*` の `*` は variants の id ではなく**、Jinja ループの `loop.index`。サーバー側は `v_price_1`, `v_price_2` … と順番に拾う
- 単一バリアント時の section 直下 input を hidden mirror する場合、JS で 2 way bind する必要あり (price/qty 双方向)
- manual override を再配置すると現在の `aa26325` の CSRF 修正対象も移動する点に注意
- variants disclosure 内の `<table>` は accessibility 上 `<table role="table">` を保つ
- モバイルで disclosure 閉じ時に `+ バリエーション追加` ボタンが行方不明にならないよう、disclosure の外に置く

### 3.6 受け入れ条件 (Definition of Done)
- [ ] variants 1 件: 販売価格・在庫が section 直下で編集できる、保存後に DB の `variants[0].price/qty` に反映
- [ ] variants 2 件以上: 従来 table が見える / 編集 / 保存できる
- [ ] manual override トグル ON で `manual_margin_rate` / `manual_shipping_cost` が DB に永続化、OFF で NULL クリア
- [ ] 「再計算して価格に反映」ボタンが現状通り動く (200 + 計算値返却)
- [ ] CSV エクスポート (`routes/export.py`) で出力が PR3 前後で同等 (画像 PR2 のサンプル `商品 1` で diff を取る)
- [ ] `pytest tests/test_e2e_routes.py -q` 95 件 全 pass
- [ ] CI 2/2 green
- [ ] product_manual_add.html の 2 カラムレイアウトに回帰なし (PR1/PR2 でも検証済の既知不変条件)

---

## 4. PR4: SEO カード独立 + 英語タイトル自動同期

### 4.1 目的
モックは SEO 独立カード + URL ハンドル/ページタイトル を `英語名と同期` バッジで `readonly` 表示。同期 OFF にすると手で書ける。

### 4.2 仕様
- 新セクション `<section>` を「販売設定」の下に挿入
- フィールド:
  - **URL ハンドル** (`name="handle"`) — `英語名と同期` toggle、ON 時 `readonly`
  - **ページタイトル** (`name="seo_title"`) — `英語名と同期` toggle、ON 時 `readonly`
  - **メタディスクリプション** (`name="seo_description"`) — 通常 textarea
- 既存「販売メモ・検索設定」section から SEO 系 3 入力を切り出して新セクションへ
- 「販売メモ・検索設定」section に残るのは `vendor` / `tags` のみ (これらは PR5 で右サイドへ移動するので一時的)

### 4.3 同期 JS のロジック
```js
// 擬似コード
const titleEnInput = document.querySelector('[name="title_en"]');
const handleInput = document.querySelector('[name="handle"]');
const seoTitleInput = document.querySelector('[name="seo_title"]');
const handleSyncToggle = document.querySelector('#handleSyncToggle');
const seoTitleSyncToggle = document.querySelector('#seoTitleSyncToggle');

function slugify(s) {
  return (s || '').toLowerCase()
    .normalize('NFKD').replace(/[\u0300-\u036f]/g, '')  // 発音記号削除
    .replace(/[^\w\s-]/g, '')   // 英数 + space + ハイフンのみ
    .trim()
    .replace(/\s+/g, '-')
    .slice(0, 80);
}

titleEnInput.addEventListener('input', () => {
  if (handleSyncToggle.checked) handleInput.value = slugify(titleEnInput.value);
  if (seoTitleSyncToggle.checked) seoTitleInput.value = titleEnInput.value.slice(0, 60);
});

handleSyncToggle.addEventListener('change', () => {
  handleInput.readOnly = handleSyncToggle.checked;
  if (handleSyncToggle.checked) handleInput.value = slugify(titleEnInput.value);
});

// seoTitleSyncToggle も同様
```

### 4.4 触るファイル
1. `templates/product_detail.html` — SEO セクション追加 + toggle UI + (`title_en` の `id` を確認、無ければ追加)
2. `static/css/style.css` — toggle pill のスタイル
3. **JS** はインラインで `product_detail.html` の `<script>` ブロックに足すか、新規 `static/js/product_seo_sync.js` を作る
4. テスト: 軽量 unit test を `tests/test_e2e_routes.py` に 1〜2 件 (form 保存時の永続化のみ。同期 JS は手動確認で十分)

### 4.5 落とし穴
- 既存 SEO フィールドの `name=` 属性は変えない (`handle`, `seo_title`, `seo_description`)
- 同期 ON 時に `readonly` だが `disabled` ではない (form submit 時の値が送られる必要あり、`disabled` だと送られない)
- `title_en` が空のときは handle/seo_title をクリアしない (ユーザーが先に書いた値を保護)

### 4.6 Definition of Done
- [ ] SEO セクションが「販売設定」の下に独立カードで表示
- [ ] 同期 ON で `title_en` 入力 → handle / seo_title が即時更新
- [ ] 同期 OFF で手入力できる
- [ ] 保存後に DB の `custom_handle`, `seo_title`, `seo_description` が反映
- [ ] CI 2/2 green

---

## 5. PR5: 右サイドカラム + Product.category + 公開カタログ category フィルタ

### 5.1 目的
- 商品編集ページの右サイドに「商品ステータス」「分類」カードを新設
- `Product.category` カラムを新規追加 (既存に存在しない)
- 公開カタログ (`templates/catalog.html`) でカテゴリーフィルタを使えるようにする

### 5.2 DB マイグレーション
- `database.py` 内の `ADDITIVE_STARTUP_MIGRATIONS` に `category` 列追加を足す

例 (`database.py` の既存パターンに合わせる):
```python
ADDITIVE_STARTUP_MIGRATIONS.append(
    AdditiveStartupMigration(
        table_name="products",
        column_name="category",
        column_type="VARCHAR",
        column_definition="VARCHAR",
        nullable=True,
        default=None,
    )
)
```

(実際の API は `database.py` を読んで合わせる。`manual_margin_rate` / `manual_shipping_cost` を追加した PR #96 の commit `fbc2434` が参考になる)

`models.Product` クラスにも `category = Column(String, nullable=True)` を追加。

### 5.3 商品編集ページの変更
1. レイアウトを 2 カラム化 (`.product-edit-layout` を `grid-template-columns: 1fr 320px` 程度に)
2. 右カラムに 2 カード:
   - **商品ステータス** カード: `status` select (既存 `name="status"` を移動)
   - **分類** カード:
     - `category` select (固定リストでよい — 例: `'トレーディングカード'`, `'フィギュア'`, `'ホビー'`, `'ファッション'`, `'家電'`, `'その他'`)
     - `vendor` 入力 (PR4 から残ってる、最終ホーム)
     - `tags` pill UI (PR4 から残ってる、最終ホーム)
3. 既存「保存前チェック」popover はヘッダ右に PR1 から残ってるので維持

### 5.4 公開カタログ側
1. `routes/catalog.py` のクエリに `?category=...` パラメータを追加
2. `templates/catalog.html` のフィルタ UI に category dropdown を追加
3. **必須**: `source_url` / `site` の漏洩がないか再確認 (AGENTS.md 高リスク不変条件)

### 5.5 product_manual_add.html 対応
- 商品手動追加にも `category` 入力を足す (一致させる)
- 既存 2 カラムグリッドが PR1 から崩れていないか必須確認

### 5.6 触るファイル
1. `database.py` (マイグレ追加)
2. `models.py` (Product モデル拡張)
3. `routes/products.py` (POST 受け取りに `category` 追加)
4. `routes/catalog.py` (公開カタログ category フィルタ)
5. `templates/product_detail.html` (右サイドカラム + status/category/vendor/tags 移動)
6. `templates/product_manual_add.html` (category 追加)
7. `templates/catalog.html` (フィルタ UI 追加)
8. `static/css/style.css` (2 カラムレイアウト + 右サイドカードのスタイル)
9. `tests/test_e2e_routes.py` (category 永続化 + 公開カタログフィルタの 2 ケース追加)

### 5.7 落とし穴
- マイグレは `ADDITIVE_STARTUP_MIGRATIONS` を必ず使う (Alembic は使ってない)
- `migrations/` を手で編集してはいけない (もしあれば; 多分このプロジェクトには無い)
- 公開カタログのテンプレで誤って `product.source_url` や `product.site` を露出させないように **`templates/catalog.html` の出力フィールドを必ずレビュー**
- `is_published` ではなく `status` で運用してる点に注意 (`status='active'` が公開、`status='draft'` が下書き)

### 5.8 Definition of Done
- [ ] 起動時マイグレで `products.category` が自動追加される (空の DB / 既存 DB 両方)
- [ ] 商品編集で category select が表示・保存できる
- [ ] 商品手動追加でも category select が表示・保存できる
- [ ] 公開カタログ `/catalog?category=トレーディングカード` でフィルタが効く
- [ ] AGENTS.md 高リスク不変条件 (公開カタログに source_url 出さない) 違反なし
- [ ] product_manual_add.html の 2 カラム回帰なし
- [ ] CI 2/2 green
- [ ] `pytest tests/test_e2e_routes.py -q` 全 pass

---

## 6. テスト戦略 (PR1/PR2 で確立済の方法を流用)

### 6.1 共通の手順
1. ローカル Flask 起動: `cd /home/ubuntu/repos/ESP && FLASK_APP=app.py flask run --host=127.0.0.1 --port=5050`
2. `tester` でログイン (password `TestUserPassword123`、テスト用ユーザー)
3. ブラウザを 1599×1034 で最大化、`/product/1` を開く (商品 1 はテスト用、`tags='vintage,black,studio'`、画像 3 件)
4. **テスト計画 .md を先に書く** (`/home/ubuntu/test_plan_pr<N>.md`) — Pass/Fail 基準を具体的な console snippet で
5. 計画通り console snippet を実行 + ブラウザ録画 + `annotate_recording`
6. 結果を `/home/ubuntu/test_report_pr<N>.md` に書く
7. PR にコメント投稿 (`git_comment_on_pr`)
8. ベースラインデータを必ず復元 (商品 1 を元の状態に)

### 6.2 ベースライン復元 SQL
```bash
cd /home/ubuntu/repos/ESP && python -c "
import sqlite3
conn = sqlite3.connect('mercari.db')
c = conn.cursor()
c.execute(\"UPDATE products SET tags='vintage,black,studio', manual_margin_rate=NULL, manual_shipping_cost=NULL, status='active' WHERE id=1\")
conn.commit()
print('restored')
conn.close()"
```

### 6.3 商品 1 の現在の baseline (PR99 テスト後)
- `id=1`
- `title='テスト商品 1 ヴィンテージ カメラ'`
- `tags='vintage,black,studio'`
- `manual_margin_rate=NULL`, `manual_shipping_cost=NULL`
- `status='active'`
- 画像 3 件 (picsum id=237, 1015, 1025)
- 最新 snapshot id=5

---

## 7. PR 作成 / レビューのお作法 (このリポジトリ固有)

### 7.1 ブランチ
- `devin/<unix_ts>-<short-name>` パターン (例: `devin/1777365876-product-edit-image-pr2`)
- 別 AI で実装する場合は適宜変えて OK (但し main からの分岐)

### 7.2 commit message
- Conventional Commits 寄り。`feat(scope):`, `fix(scope):`, `refactor(scope):` を使う
- 例: `refactor(product-edit): PR3 sales section independence + variant table disclosure`

### 7.3 PR 説明本文
- スコープ・変更点・残課題を箇条書き
- "out of scope: PR4 で SEO カード独立、PR5 で右サイドカラム" のように依存関係を明示

### 7.4 CI で必ず通るもの (この repo)
- `pytest tests/test_e2e_routes.py -q` (95 件)
- `pytest tests/test_worker_runtime.py -q`
- `pytest tests/test_worker_entrypoint.py -q`
- 多分 lint も走る (`ruff` / `black` / `mypy` どれか — `pyproject.toml` か `Makefile` 確認)

### 7.5 Devin Review (自動レビュアー)
- PR 作成すると `https://app.devin.ai/review/...` の自動レビューが走る
- 指摘されたら commit を追加 (`--amend` しない) で対応 → PR ページにコメント返信

### 7.6 マージ
- ユーザー (`halc8312`) が手動でマージする運用
- AI 側はマージしない

---

## 8. PR1 で残った既知の軽微な所見 (実害なし、PR3 で解消する想定)

- `templates/product_detail.html` の variants section 内に空 `<details><summary></summary></details>` が 1 件残存している
  - `loop.index 1` の場所
  - PR3 の variants disclosure で正式に使うので、PR3 で削除 or 置換 OK

---

## 9. もしさらに先のフェーズに進むなら (5 PR 完了後)

ユーザーが「PayPal は別 PR で」と明言しているので、5 PR 完了後の候補として:

1. **PayPal 決済 + 在庫連動制御** (元々の最大要件、AGENTS.md `Not yet implemented` に挙がっている)
   - `Shop.paypal_email` カラム
   - 設定 UI (`/settings`)
   - サーバーサイド価格再計算 API
   - 公開商品ページの Buy ボタン
   - `Order` モデル
   - PayPal Webhook
   - 在庫鮮度確認
   - 詳細は `/home/ubuntu/260322_gap_analysis.md` を参照

2. **その他のカタログ A/B 微調整** (現状ほぼ同等)

これは別 issue で要件定義から始めることになる。

---

## 10. ローカル開発環境セットアップ (引き継ぎ先 AI 用)

```bash
# 1. clone
git clone https://github.com/halc8312/ESP.git
cd ESP

# 2. Python 仮想環境
python -m venv .venv
source .venv/bin/activate

# 3. 依存関係 (pyproject.toml or requirements.txt を確認)
pip install -e ".[dev]"   # または pip install -r requirements.txt

# 4. DB セットアップ
flask db-init  # またはアプリ起動時に自動実行されるはず
# テストデータ投入は適宜 (cli.py に seed コマンドあるか確認)

# 5. テストユーザー作成 (cli.py)
flask create-user tester TestUserPassword123 --email tester@example.com

# 6. 起動
FLASK_APP=app.py flask run --host=127.0.0.1 --port=5050

# 7. ログイン
# ブラウザで http://127.0.0.1:5050/login → tester / TestUserPassword123
```

セットアップで詰まったら `README.md` と `cli.py` を参照。

---

## 11. 直近のオープンタスク (このまま PR3 から進めれば OK)

- [ ] PR #99 をユーザーがマージ (マージ後に PR3 着手)
- [ ] PR3: 販売設定 independence + variant table disclosure + single-variant fallback
- [ ] PR4: SEO card + English title auto-sync JS
- [ ] PR5: Right sidebar + Product.category migration + public catalog category filter
- [ ] (任意) PR2 終了に合わせて `.agents/skills/testing-product-edit/SKILL.md` に PR2 用の §3.5 busy glyph 確認手順や §5 reorder 永続化手順を追記して、未来の AI が再現できるようにする

---

## 12. 連絡事項 (引き継ぎ先 AI へ)

- ユーザーは **日本語で会話**しています。返信も日本語で OK
- ユーザーはリスクの高い変更 (`category` カラム追加など) は本人が手元でローカルテストする派なので、PR を出して手元で動作確認してもらう前提で OK
- ユーザーは「この内容で進めましょう」スタイルで承認するタイプ。曖昧な提案には反応しないので、**PR 単位で具体的なスコープを提示** → OK もらう → 実装の流れが速い
- E2E テストは PR ごとに毎回お願いされる可能性が高い (PR1/2/96 全部依頼された)。**PR 作成時に `offer_to_test_app=true` を付ける**のがこの repo の運用
- 「マージしました」と言われたら次の PR に着手して OK

---

## 13. 重要なコミット参照表

| commit | 内容 | 引用しやすい補足 |
|---|---|---|
| `0a3d3eb` | bg-removal busy glyph fix (`'処理中'` → `'⟳'`) | PR2 review 対応 |
| `9b686ee` | `[hidden]` overlay の display: none を `!important` で強制 | PR2 review 対応 |
| `e211f97` | PR2 image section 10-col grid 本体実装 | PR2 メイン |
| `c497567` | PR1 single-column layout を product_detail のみにスコープ | PR1 review 対応 |
| `8868a33` | popover icon 22px (`min-width: 42px` 継承を阻止) | PR1 review 対応 |
| `9340fab` | popover DOM 破壊回避 + class 名整合 | PR1 review 対応 |
| `ec7b2da` | PR1 foundation refactor 本体 | PR1 メイン |
| `aa26325` | recalc-price fetch に `X-CSRFToken` 追加 | PR #96 テスト中発見 |
| `4bfa8a7` | manual override only 商品も background recalc 対象に | PR #96 review 対応 |
| `18c294f` | `/api/product/<id>/recalc-price` → `/api/products/<id>/...` | API 命名規則統一 |
| `d714d27` | `update_product_selling_price` で manual override-only もリカバリ | PR #96 review 対応 |
| `d90467a` | background 再計算で `manual_margin_rate` / `manual_shipping_cost` を尊重 | PR #96 review 対応 |
| `fbc2434` | PR #96 メイン (タグ pill / lightbox / manual override) | PR #96 メイン |

---

## 14. 質問が出る前に答えておく FAQ

**Q: PR3 で variants table を完全に廃止してしまっていい?**
A: ダメ。eBay/Shopify CSV エクスポートが variants 構造前提なので、disclosure に格下げしつつ DB スキーマと CSV 出力は維持する。

**Q: モックの単一バリアント UX に完全に寄せるには?**
A: 寄せきると CSV エクスポートが壊れる。「1 件のときは section 直下に、2 件以上は disclosure 開いて table に」のハイブリッド方式 (本ドキュメント §3.2) で合意済。

**Q: PR5 の category select はマスタテーブル化しなくていい?**
A: 初版は固定リストでよい (本ドキュメント §5.3 の 6 つの選択肢)。マスタ化は将来のリファクタ範囲。

**Q: SEO 同期 JS で `title_en` が空のときにスラグをクリアしていい?**
A: NO。ユーザーが先に書いた handle/seo_title を保護するため、空のときは何もしない (PR4 §4.5 落とし穴参照)。

**Q: PR3 で manual override の保存ロジックを変えてもいい?**
A: 変更不要。既存の `routes/products.py` の `manual_margin_rate` / `manual_shipping_cost` 受け取りはそのまま流用できる (form name が同じなら)。

**Q: 公開カタログ category フィルタで複数選択は必要?**
A: 初版は単一選択でよい。複数選択は将来。

**Q: 状態 `status` を boolean `is_published` に戻したい?**
A: 戻さない。すでに `status VARCHAR` で運用してる。`active` / `draft` の 2 値で十分。

---

## 15. ファイル添付一覧 (引き継ぎ時に渡すべきもの)

引き継ぎ先 AI のセッションを開く時に、以下のファイルを添付すると速い:

1. **本ドキュメント** `/home/ubuntu/HANDOFF_ESP_product_edit_redesign.md`
2. ギャップ分析 `/home/ubuntu/260427_product_edit_gap_analysis.md`
3. 残すべき機能 `/home/ubuntu/260427_must_keep_list.md`
4. 商品編集モック (PSA10) `/home/ubuntu/attachments/c6b46db6-937c-4b15-aca2-15bd6f76db82/.html`
   または `/home/ubuntu/attachments/4b81a64d-91c0-4c94-855b-8d08547bc25c/.html`
5. PR2 テスト計画 `/home/ubuntu/test_plan_pr99.md` (PR3/4/5 の計画書テンプレに使える)

---

以上。本ドキュメントを起点に PR3 → PR4 → PR5 の順に進めれば、ユーザーが期待する完成形に到達できる構造になっています。

引き継ぎ先 AI への一言: **「PR2 (#99) のマージ確認 → PR3 ブランチ切り → 本ドキュメント §3 を読み込んで実装開始」** がスタート手順です。Good luck!
