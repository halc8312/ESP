# Test report — PR #97 (商品編集 PR1: foundation refactor)

PR: https://github.com/halc8312/ESP/pull/97 (commit `c497567`)
Test plan: `/home/ubuntu/test_plan_pr97.md`

## Summary

ローカルの Flask dev (port 5050) で `tester` ログイン、商品 id=1 (`mercari` / last_price=2000 / source_url=`https://example.com/item/1`) を対象に E2E 実機テスト。**実行した 7 ブロックすべて PASS**。Devin Review で潰した 4 つの回帰 (popover DOM 破壊 / CSS class 名 / 22px / `product_manual_add` 2 カラム) も実機で再確認済み。テスト後は test data 復元済 (`tags=vintage,black,studio`, `manual_margin_rate=NULL`, `manual_shipping_cost=NULL`)。

## エスカレーション (前もって)

軽微 (機能には影響しない) ですが共有しておきます:

- **`<details>` が 1 件残っている** (variants section 内の空タグ `<details><summary></summary></details>`): PR1 で `<details class="product-edit-panel">` accordion を全廃する目的は果たしているが、別目的の空 `<details>` が 1 件残存していた。中身は空で UI には何も出ていない。PR3 (variant disclosure 化) でこのスロットを正式に使う想定なので、PR1 ではあえて残す判断で OK。

## 実行結果 (test plan §順)

### §1 sticky header
- **§1.1** PASS: header に 6 要素 (back-arrow `‹` / `<h1>` / `#saveStateBadge=保存済み` / `#heroStatusBadge=公開中 (is-active)` / `#heroReadinessBadge` / `<button form="productEditForm">保存する</button>`) が揃い、`position: sticky` / `top: 0px` / `z-index: 30` を確認
- **§1.2** PASS: `window.scrollY = 2712` まで縦スクロールしても header `getBoundingClientRect().top = 0` で pin されている
- **§1.3** PASS: `保存する` ボタンは `<form id="productEditForm">` の **外** にあり `form="productEditForm"` 属性で submit が結線されている (実際にクリックして round-trip 成功)

### §2 readiness popover
- **§2.1** PASS: 初期状態 `data-ready=todo` / `チェック 4/5` (description は server-side で空判定だが TinyMCE 反映後 5/5 へ遷移するのを確認)
- **§2.2** PASS: hover で popover に 5 件の `<li data-check-key>` (images / title / description / variants / price) が表示。**Devin Review で指摘された popover DOM 破壊バグの再発なし** (textContent 上書きではなく `#heroReadinessText` span のみ更新する `9340fab` 修正が実機で機能)
- **§2.3** PASS: popover 内の `.product-edit-check-icon` 5 個すべて `getBoundingClientRect()` で `22 × 22 px` を計測。**Devin Review で指摘された `min-width: 42px` 継承バグの再発なし** (`8868a33` 修正)
- **§2.4** PASS (live update 検証): タイトル input をクリア → タイトル popover item が `is-complete` を失い `OK→要` に遷移、その直後タイトル復元で `5/5` (全 OK) に戻ることを確認。最初は count が `4/5→4/5` で変化しないように見えたが、これは TinyMCE が遅延初期化で description を空判定していたものが直前で OK に遷移し、title が要に変わるのと相殺してたためと判明。生の readiness DOM 各 5 アイテムは正しく流動している

### §3 仕入設定 (自動取得) read-only card
- **§3.1** PASS: `<section id="productSourcePanel">` が `<form>` の外側 (.product-edit-layout の上) に配置
- **§3.2** PASS: 5 件の `.product-source-summary-row` (取得元サイト / 仕入れ価格 (記録) / 仕入れ元の状態 / 最後に取得したタイトル / 取得元URL)、最後 2 件は `is-wide`
- **§3.3** PASS: 値も完全一致
  - 取得元サイト: `mercari`
  - 仕入れ価格 (記録): `¥2,000`
  - 仕入れ元の状態: `on_sale`
  - 最後に取得したタイトル: `テスト商品 1 ヴィンテージ カメラ`
  - 取得元URL: `<a href="https://example.com/item/1" target="_blank">…</a>`
- **§3.4** PASS: `<span class="product-edit-summary-meta">読み取り専用</span>` を確認、`#productSourcePanel` 内の `<input>/<textarea>/<select>` カウント = **0**

### §4 save round-trip (header `保存する` ボタン)
- **§4.1–4.3** PASS: ヘッダ `保存する` (form 属性経由) クリック → POST `/products/1/edit` → 302 → 再表示。再表示後の DOM:
  - `pr97test` タグ pill が表示中の 4 件目として復活 (`vintage`, `black`, `studio`, `pr97test`)
  - `manual_margin_rate=25` 永続化
  - `manual_price_enabled` checked 状態
  - title はそのまま `テスト商品 1 ヴィンテージ カメラ`
  - `#saveStateBadge=保存済み`
- **§4.4** PASS: 直後に SQL で test data 復元済 (`tags='vintage,black,studio'`, manual_margin_rate=NULL, manual_shipping_cost=NULL)

### §5 `<details>` accordion 全廃
- **§5.1** PARTIAL: form 内の `.product-edit-panel` style `<details>` は **0** で OK (=PR の意図は満たしている) が、variants section 内に空の `<details><summary></summary></details>` が **1 件残存** (上述エスカレーション参照)。実害なし
- **§5.2** PASS: `<section>` カードが 5 件 (基本設定 / 商品画像 / 商品説明 / バリエーション設定 / 販売メモ・検索設定) すべて存在
- **§5.3** PASS: Beginner-Friendly Flow ヒーロー (`.beginner-flow-hero`) は DOM に存在しない

### §6 shared CSS regression — `product_manual_add.html` 2 カラム維持
- **§6.1–6.2** PASS (1599×1034 viewport): `.product-edit-layout` `gridTemplateColumns = "745.469px 438.531px"` (2 トラック)、`.product-edit-side` `position: sticky` / `top: 88px` / 左座標 1031px (右側) / 幅 439px。**Devin Review で指摘された CSS regression の再発なし** (`c497567` 修正)
- **§6.3** PASS (mobile 410×800): `gridTemplateColumns = "380px"` (1 トラック) に collapse、横スクロールなし

### §7 console errors / warnings
- **§7.1** PASS: ページロード + §2.4 / §4 の編集中 + ヘッダ保存中、`console.error` および `window.onerror` で収集した uncaught exceptions = **0 件**

### §8 mobile (≤640px) sticky header
- 別途 ≤640px での独立検証は未実施 (UNTESTED) が、§6.3 で mobile breakpoint の grid collapse は確認済み。header 単体のモバイル確認は PR2 以降で recordingに含める予定

### §9 status chip color
- **§9.1** PASS (initial state): `#heroStatusBadge` のクラスは `product-edit-chip product-edit-status-chip is-active`、表示は `公開中`
- **§9.2** UNTESTED: status を draft に切り替えての挙動はマイグレ無しで検証可能だが今回時間優先で省略

## Devin Review 指摘 4 件の実機再確認

| # | 指摘 | 修正コミット | 実機確認 |
|---|---|---|---|
| 1 | CSS class `.check-mark` が無く `.product-edit-check-icon` を参照すべき | `9340fab` | §2.3 で 22px 化が機能 |
| 2 | `heroReadinessBadge.textContent =` が popover DOM を破壊 | `9340fab` | §2.2 で popover 5 アイテム表示 |
| 3 | popover icon が `min-width: 42px` 継承で 22px にならない | `8868a33` | §2.3 で 22×22px 計測 |
| 4 | 共通 CSS の grid 削除が `product_manual_add.html` を破壊 | `c497567` | §6.1 で 2 トラック復活確認 |

## Untested

- §3.5 public catalog source_url leak — このリポジトリ既存の高リスク不変 (AGENTS.md) で別途 e2e (`tests/test_e2e_routes.py`) でカバーされており、今回は再ナビゲートまで含めず CI に委譲
- §8 mobile (≤640px) でのヘッダの折り返し
- §9.2 status を draft に切り替えての chip class 切り替え

## CI

最新 commit `c497567` で GitHub Actions 2/2 通過済 (`Lint and Test (3.11)` / `Lint and Test (3.12)`)。`pytest tests/test_e2e_routes.py -q` 95 件 pass。

## Test data restore

```sql
UPDATE products SET tags='vintage,black,studio', manual_margin_rate=NULL, manual_shipping_cost=NULL WHERE id=1;
-- BEFORE: tags='vintage,black,studio,pr97test', manual_margin_rate=25
-- AFTER:  tags='vintage,black,studio', manual_margin_rate=NULL
```

(SQL 復元実施済)
