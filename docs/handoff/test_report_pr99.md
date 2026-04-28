# PR #99 (PR2 商品画像 10-col グリッド) E2E テストレポート

- PR: https://github.com/halc8312/ESP/pull/99 (commit `0a3d3eb`、CI 2/2 green)
- 対象: `templates/product_detail.html` 商品画像セクション全面リデザイン + Sortable / lightbox / bg-removal preview / busy glyph 回帰
- テスト日時: 2026-04-28 (UTC)
- ベースライン復元: 完了 (`tags='vintage,black,studio'`, `manual_*=NULL`, image 順 `[237,1015,1025]`)

## 実施方法

ローカル Flask dev (127.0.0.1:5050) を起動し `tester` で `/product/1` を開いて、
- console snippet で DOM 構造 / computed style / Sortable config / form hidden input / 配列 splice → renderImageList → form.requestSubmit() round-trip を assert
- viewport を desktop (1599×1034) と mobile (410w) で切替えて grid track 数を assert
- `/products/manual-add` で 2 カラム回帰を assert
- `annotate_recording` で各テストの開始/結果を録画オーバーレイに記録

を行いました。実機マウスドラッグの代わりに `imageUrls.splice()` + `renderImageList()` + `form.requestSubmit()` で `onEnd` ハンドラと等価のパスを通し、ブラウザを再 navigate して `image_urls_json` が DB から復元されることを実機で確認しています。

## 結果一覧

| §  | テスト | 結果 |
|----|------|------|
| §1.1 | 新 DOM (旧 body/handle/order/url 要素なし、`+追加` タイル末尾) | PASS |
| §1.2 | 各カードに hover actions / bg-status / bg-actions overlay 存在 | PASS |
| §2.1 | desktop (1599w) → 10列 (`grid-template-columns` tracks=10) | PASS |
| §2.3 | mobile (410w) → 5列 | PASS |
| §2.4 | サムネ aspect-ratio ≈ 1:1 | PASS |
| §3.1 | 静止時 `.product-image-card-actions` opacity=0 / pointer-events=none | PASS |
| §3.2 | focus 中 opacity=1, blur 後 opacity=0 (`:focus-within` で代替検証) | PASS |
| §3.3 | 円形アクションボタン 24×24 px | PASS |
| §3.4 | actions の z-index=2 (thumb の上に来る) | PASS |
| §3.5 | **NEW**: busy glyph `'⟳'` が 24×24 内に収まる (回帰テスト for `0a3d3eb`) | PASS |
| §4.1 | thumb クリックで lightbox open + 正しい src | PASS |
| §4.2 | アクションボタン click は lightbox を開かない | PASS |
| §4.3 | `+追加` タイル click は lightbox を開かない | PASS |
| §5.1 | Sortable: `draggable: '.product-image-card'`, filter に add tile / actions ボタン | PASS |
| §5.2 | imageUrls splice → image_urls_json hidden input に新順序が反映 | PASS |
| §5.3 | form.requestSubmit() round-trip → reload 後に image_urls_json が新順序で復元 | PASS |
| §5.4 | 並べ替えを baseline `[237,1015,1025]` に戻して保存 | PASS |
| §6.1 | `+追加` タイル click が `imageFileInput.click()` をトリガー | PASS |
| §6.2 | `+追加` タイルは `.product-image-card` に match しない (Sortable から除外) | PASS |
| §7.1 | bg status badge `position: absolute` + 表示可 | PASS |
| §7.2 | bg apply/reject 行 `position: absolute`, `bottom: 4px` | PASS |
| §8 | console errors 0 件 | PASS |
| §9 | `/products/manual-add` で 2 カラム grid (1599w) / 1 カラム (mobile) 維持 | PASS |
| §10 | ベースライン復元 (tags / manual_* / image 順) | PASS |

untested: 実機マウスドラッグでの並べ替え (browser tool で DnD イベントを発火しても Sortable.js の動作トリガーに不十分なため、splice + onEnd 等価パスで代替検証)。

## エビデンス

スクリーンショット: 商品 1 の編集画面。商品画像セクションが 10 列グリッド (3 サムネ + 追加タイル) で、サムネが正方形、`+追加` がグリッドの末尾に位置する状態。

![screenshot](screenshots/127_5050_product_115754.png)

録画: `rec-bd6c008a70354a0d8e3d1cd7466234c0-edited.mp4` (各テストの開始・結果を `annotate_recording` で字幕表示)

## 中で見つけた回帰修正 (本テスト前 commit `0a3d3eb`)

PR review でレビュアーから flag 受けた `setButtonBusy` の textContent='処理中' (3 char) が 24×24 ボタンを overflow する件は、本テスト直前に `'⟳'` (single spinner char) に変更済 (`static/js/product_bg_removal.js:63`)。§3.5 で実機検証し fits=true を確認。

## ベースライン復元状況

```
products row 1: tags='vintage,black,studio', manual_margin_rate=NULL, manual_shipping_cost=NULL
latest snapshot images: [237, 1015, 1025]
```

## 結論

PR #99 はモック準拠の 10-col 画像グリッド、hover 円形アクション、Sortable によるドラッグ対象の差し替え、ライトボックス click guard、bg-removal preview overlay、`+追加` タイル の全要件を満たしており、`product_manual_add.html` の既存 2 カラムレイアウトに対して回帰なし、console error 0 件、image 並び替えは form submit で永続化されることを実機確認しました。マージ可能です。
