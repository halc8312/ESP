# 商品編集リデザイン 引き継ぎ資料

このフォルダは、Devin が進めていた **商品編集ページのリデザイン (5 段階 PR 計画)** を別の AI ツールに引き継ぐための資料一式です。

## 起点ドキュメント

まずこれを読む:

- [`HANDOFF_ESP_product_edit_redesign.md`](./HANDOFF_ESP_product_edit_redesign.md) — **メインの引き継ぎ書**。完了済 PR / 残り PR (PR3/PR4/PR5) の詳細仕様・落とし穴・受け入れ条件・テスト戦略・FAQ をまとめた中心ドキュメント。

## 補助ドキュメント (引き継ぎ書から参照される)

| ファイル | 目的 |
|---|---|
| [`product_edit_gap_analysis.md`](./product_edit_gap_analysis.md) | 商品編集モック (PSA10) vs 現状コードの詳細ギャップ分析 |
| [`must_keep_list.md`](./must_keep_list.md) | 既存機能のうち「絶対残すべき」「廃止可」「新規必要」の分類リスト |
| [`260322_gap_analysis.md`](./260322_gap_analysis.md) | 初回 (3/22 提出資料) のギャップ分析 — PayPal 含む全体像 |

## 過去 PR の E2E テスト計画 / 結果 (PR3 以降のテスト雛形に使う)

| PR | 計画 | 結果 |
|---|---|---|
| #96 (カタログ list / タグ pill / lightbox / 個別価格 override) | [`test_plan_pr96.md`](./test_plan_pr96.md) | [`test_report_pr96.md`](./test_report_pr96.md) |
| #97 (PR1: foundation refactor) | [`test_plan_pr97.md`](./test_plan_pr97.md) | [`test_report_pr97.md`](./test_report_pr97.md) |
| #99 (PR2: 画像 10-col グリッド) | [`test_plan_pr99.md`](./test_plan_pr99.md) | [`test_report_pr99.md`](./test_report_pr99.md) |

## 進捗状況 (2026-04-28 時点)

- [x] **PR #96** マージ済 — 高密度カタログ list / タグ pill / 画像ライトボックス / 個別価格オーバーライド
- [x] **PR #97 (PR1)** マージ済 — `<details>` 廃止 + `<section>` 化 + sticky header + 仕入設定カード
- [x] **PR #98** マージ済 — testing-product-edit SKILL 追加
- [x] **PR #99 (PR2)** マージ済 — 商品画像 10 列グリッド + hover 円形アクション + Sortable + bg-removal preview
- [ ] **PR3** — 販売設定 independence + variant table disclosure + single-variant fallback (**次の着手ポイント**)
- [ ] **PR4** — SEO カード独立 + 英語タイトル → URL ハンドル/SEO タイトル 自動同期 JS
- [ ] **PR5** — 右サイドカラム + `Product.category` カラム追加 + 公開カタログ category フィルタ

## 引き継ぎ先 AI へのスタートガイド

1. [`HANDOFF_ESP_product_edit_redesign.md`](./HANDOFF_ESP_product_edit_redesign.md) を最初から最後まで読む
2. §3 (PR3 仕様) を熟読
3. ユーザー (`halc8312`) のモック HTML を再添付してもらう (PSA10 ワンピースカード商品編集)
4. `main` から `devin/<ts>-product-edit-sales-pr3` を切る
5. PR3 を実装 → CI green → PR 作成 → ユーザーに E2E テストを提案 (`offer_to_test_app=true`)
6. マージされたら PR4 へ

詳細は引き継ぎ書本体を参照してください。
