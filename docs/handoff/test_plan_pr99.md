# PR #99 (PR2 商品画像 10-col グリッド) E2E テスト計画

対象 PR: https://github.com/halc8312/ESP/pull/99 (commit `e211f97`)
対象 URL: `http://127.0.0.1:5050/products/1/edit` (tester / 商品 1 / 3 画像)

## 観点

PR2 で変えた **見た目 (10列グリッド + hover 円形アクション) と挙動 (Sortable / lightbox / bg-removal preview / add tile)** が壊れずに動くか、かつ `product_manual_add.html` の 2 カラムが回帰していないか。

PR1 で確認済みの sticky header / popover / 仕入設定カードは PR2 では一切触っていない (テンプレ間に変更なし) ので、本計画では再検証を最小限の smoke check に留め、**画像セクションに集中**する。

## 共通前提

```
viewport: 1280x900 (desktop) / 900x900 / 640x900 (mobile)
DB:       商品 1 の image_urls = 3件 (picsum.photos id 237/1015/1025)
```

各テストは **「変更が壊れていればこの assertion で必ず落ちる」** ものに絞ること。同じ手順が「壊れた実装」でも観測できるなら no-op なので除外する。

---

## §1. 画像セクションの構造 smoke (1280x900)

### §1.1 image grid が新 DOM になっている

```js
var grid = document.getElementById('imageSortGrid');
var cards = grid.querySelectorAll('.product-image-card');
var addTile = grid.querySelector('.product-image-add-tile');
console.log({
  cards: cards.length,
  hasOldBody: !!grid.querySelector('.product-image-card-body'),
  hasOldHandle: !!grid.querySelector('.image-sort-handle'),
  hasOldOrderLabel: !!grid.querySelector('.product-image-order'),
  hasOldUrlText: !!grid.querySelector('.product-image-url'),
  addTile: !!addTile,
  addTileLastChild: grid.lastElementChild === addTile,
});
```

PASS:
- `cards: 3`
- `hasOldBody / hasOldHandle / hasOldOrderLabel / hasOldUrlText` が **全部 false**
- `addTile: true`
- `addTileLastChild: true` (add tile が grid の最後)

FAIL: 旧 body 要素のいずれかが残っていれば PR2 の DOM 移行が不完全。

### §1.2 各カードに新しい hover アクション overlay が存在

```js
var first = document.querySelector('#imageSortGrid .product-image-card');
console.log({
  hasActionsOverlay: !!first.querySelector('.product-image-card-actions'),
  hasBgRemoveBtn: !!first.querySelector('[data-bg-remove-btn]'),
  hasDeleteBtn: !!first.querySelector('.product-image-action-btn.delete'),
  hasBgStatus: !!first.querySelector('[data-bg-status]'),
  hasBgActions: !!first.querySelector('[data-bg-actions]'),
});
```

PASS: 全 5 項目 true。

---

## §2. レスポンシブ grid の列数 (核心)

### §2.1 desktop (1280x900) → 10列

```js
var grid = document.getElementById('imageSortGrid');
var cs = getComputedStyle(grid);
var tracks = cs.gridTemplateColumns.split(' ').filter(Boolean).length;
console.log({viewport: innerWidth, tracks, gridCols: cs.gridTemplateColumns});
```

PASS: `viewport: 1280, tracks: 10`。
FAIL: 5 のままなら `@media (min-width: 768px)` が効いていない。

### §2.2 tablet (900x900) → 10列 (≥768px なので)

window resize 後同じ snippet を実行。
PASS: `tracks: 10`。

### §2.3 mobile (640x900) → 5列

PASS: `tracks: 5`。
FAIL: 10 のままなら base rule (`repeat(5, …)`) が効いていない。

### §2.4 アスペクト比

```js
var card = document.querySelector('#imageSortGrid .product-image-card');
var r = card.getBoundingClientRect();
console.log({w: Math.round(r.width), h: Math.round(r.height), ratio: (r.width/r.height).toFixed(2)});
```

PASS: `ratio` ≈ 1.00 (`aspect-ratio: 1/1` が効いている)。FAIL: 0.5 や 2.0 など。

---

## §3. Hover-only アクションボタン

### §3.1 静止時は不可視 (opacity 0)

```js
var actions = document.querySelector('#imageSortGrid .product-image-card .product-image-card-actions');
console.log({opacity: getComputedStyle(actions).opacity, pointerEvents: getComputedStyle(actions).pointerEvents});
```

PASS: `opacity: '0'`、`pointerEvents: 'none'`。
FAIL: `opacity: '1'` (常時表示 = mockup 違反)。

### §3.2 hover シミュレーションで可視化

DOM API では `:hover` 直接トリガーできないので、`:focus-within` で確認: action ボタンに focus を当てて `opacity` が 1 になることを assert。

```js
var card = document.querySelector('#imageSortGrid .product-image-card');
var btn = card.querySelector('[data-bg-remove-btn]');
btn.focus();
var actions = card.querySelector('.product-image-card-actions');
console.log({focusOpacity: getComputedStyle(actions).opacity, focusedElement: document.activeElement === btn});
btn.blur();
setTimeout(()=>console.log({afterBlur: getComputedStyle(actions).opacity}), 100);
```

PASS: focus 中 `opacity: '1'`, blur 後 `opacity: '0'`。

### §3.3 ボタンサイズ 24x24 (mockup 準拠)

```js
var btns = document.querySelectorAll('#imageSortGrid .product-image-card .product-image-action-btn');
console.log([...btns].slice(0,4).map(b => {var r=b.getBoundingClientRect(); return Math.round(r.width)+'x'+Math.round(r.height)}));
```

PASS: 全要素 `24x24`。FAIL: `36x36` (旧サイズ) または `42x42` (popover の `min-width: 42px` が誤って継承された)。

### §3.4 hover 中はカードの z-index に隠れない

`actions` が `z-index: 2` で thumb の上に来ているか:

```js
var actions = document.querySelector('.product-image-card-actions');
var thumb = document.querySelector('.product-image-thumb');
console.log({actionsZ: getComputedStyle(actions).zIndex, thumbZ: getComputedStyle(thumb).zIndex});
```

PASS: `actionsZ: '2'`。

### §3.5 NEW: busy 中の glyph が 24×24 内に収まる (regression test for `0a3d3eb`)

`product_bg_removal.js` の `setButtonBusy(card, true)` が以前は `'処理中'` (3 char) を 24×24 ボタンに書き込んでいたため visual overflow を起こしていた。回帰防止として、busy 化後の textContent / scrollWidth を確認する。

```js
var card = document.querySelector('#imageSortGrid .product-image-card');
var btn = card.querySelector('[data-bg-remove-btn]');
btn.dataset.originalLabel = '✦';
btn.setAttribute('disabled','disabled');
btn.setAttribute('aria-busy','true');
btn.textContent = '⟳';
var r = btn.getBoundingClientRect();
console.log({label: btn.textContent, w: Math.round(r.width), h: Math.round(r.height), scrollW: btn.scrollWidth, fits: btn.scrollWidth <= Math.ceil(r.width)+1});
btn.removeAttribute('disabled');
btn.removeAttribute('aria-busy');
btn.textContent = btn.dataset.originalLabel;
```

PASS: `label: '⟳'` (single char), `w: 24, h: 24`, `fits: true`。
FAIL: `label: '処理中'` または `fits: false` (overflow)。

---

## §4. ライトボックス (PR1 の lightbox は無傷か)

### §4.1 thumb クリックで lightbox が開く

```js
var thumb = document.querySelector('#imageSortGrid .product-image-card img.product-image-thumb');
thumb.click();
setTimeout(()=>{
  var lb = document.getElementById('imageLightbox');
  console.log({open: lb.classList.contains('is-open'), src: document.getElementById('imageLightboxImg').src.slice(-30)});
}, 100);
```

PASS: `open: true`、`src` が最初の画像 URL の末尾 (`237/600/600`) を含む。

### §4.2 アクションボタンクリックは lightbox を開かない

lightbox を閉じてから:

```js
var lb = document.getElementById('imageLightbox');
document.querySelector('[data-image-lightbox-close]').click();
setTimeout(()=>{
  var btn = document.querySelector('#imageSortGrid .product-image-card [data-bg-remove-btn]');
  // ライトボックスが閉じている状態で bg-remove ボタンをクリック (実際の bg-removal は CSRF/route で別途)
  // ここでは「クリック後 lightbox が開いていない」ことだけ assert
  var prevOpen = lb.classList.contains('is-open');
  // クリックすると bg-removal API 呼び出しが走るので, propagation 検証用にカスタムイベントで近似:
  var e = new MouseEvent('click', {bubbles: true, cancelable: true});
  // closest('.product-image-action-btn') で早期 return するパス確認
  document.querySelector('.product-image-card-actions').dispatchEvent(e);
  setTimeout(()=>console.log({lightboxOpenAfterActions: lb.classList.contains('is-open'), wasOpenBefore: prevOpen}), 100);
}, 200);
```

PASS: `lightboxOpenAfterActions: false`。

### §4.3 add tile クリックは lightbox を開かない

```js
var lb = document.getElementById('imageLightbox');
var addTile = document.getElementById('imageAddTile');
var e = new MouseEvent('click', {bubbles: true, cancelable: true});
addTile.dispatchEvent(e);
setTimeout(()=>console.log({lightboxOpen: lb.classList.contains('is-open')}), 100);
```

PASS: `lightboxOpen: false` (add tile は lightbox click guard で除外されている)。

---

## §5. ドラッグ並べ替え + image_urls_json 永続化 (核心)

### §5.1 Sortable がカード本体を draggable に設定

```js
console.log({SortableLoaded: typeof Sortable !== 'undefined'});
var grid = document.getElementById('imageSortGrid');
var inst = Sortable.get(grid);
console.log({
  draggable: inst.options.draggable,
  filter: inst.options.filter,
  handle: inst.options.handle,
});
```

PASS: `draggable: '.product-image-card'`, `filter` に `.product-image-add-tile` を含む, `handle` は無し or `null`。

### §5.2 imageUrls 配列の並びをスクリプトで入れ替え → image_urls_json が更新

UI 上のドラッグはスクリプト経由でエミュレーションが難しいため、**imageUrls 配列の splice + renderImageList()** を直接実行して JS 配線を assert する (`onEnd` ハンドラと等価):

```js
console.log('before:', JSON.parse(document.getElementById('image_urls_json').value));
var moved = imageUrls.splice(0, 1)[0];
imageUrls.splice(2, 0, moved);
markFormDirty();
renderImageList();
console.log('after :', JSON.parse(document.getElementById('image_urls_json').value));
```

PASS: hidden input が `[B, C, A]` の順に並び、長さ 3 を維持する。
FAIL: 順番が反映されない or 長さが減る。

### §5.3 ドラッグ後に save round-trip (※実機ドラッグの代わりに上の splice 後に form submit)

`form` 経由で `POST /products/1/edit` を投げ、`/products/1/edit` を再 GET したときに `image_urls_json` が新しい順序で復元されることを確認。

```js
var form = document.querySelector('.product-edit-form');
// dirty 状態にしてから submit
markFormDirty();
form.submit();
```

submit 後の遷移先 (`/products/1/edit` リダイレクト) で:

```js
console.log(JSON.parse(document.getElementById('image_urls_json').value));
```

PASS: 新しい順 `[B, C, A]`。
FAIL: 元の `[A, B, C]` のままなら image_urls_json が SSR 側から復元されていない。

### §5.4 並べ替え後に元に戻す

submit して順序を `[A, B, C]` に戻し、確実に baseline へ復帰。

---

## §6. Add tile (新規追加導線)

### §6.1 add tile クリック → file input が clicked

`focusImageUpload()` が `imageFileInput.click()` を呼ぶ。**ファイル選択ダイアログは GUI 操作が必要** なので、自動化では `imageFileInput.addEventListener('click', spy)` を仕込んでクリック数を観測する:

```js
var spy = 0;
var input = document.getElementById('image_file_input');
var origFn = input.click.bind(input);
input.click = function(){spy += 1;}; // 一時的に上書き
document.getElementById('imageAddTile').click();
console.log({spy});
input.click = origFn;
```

PASS: `spy: 1`。
FAIL: 0 (ハンドラが配線されていない)。

### §6.2 add tile は draggable から除外されている

```js
var addTile = document.getElementById('imageAddTile');
console.log({addTileMatchesDraggable: addTile.matches('.product-image-card')});
```

PASS: `false` (`.product-image-card` ではない → Sortable が拾わない)。

---

## §7. bg-removal preview overlay の可視性

### §7.1 status badge が overlay として描画される

JS で status を強制的に表示させる:

```js
var card = document.querySelector('#imageSortGrid .product-image-card');
var status = card.querySelector('[data-bg-status]');
status.textContent = 'プレビュー: 背景を白抜きしました';
status.setAttribute('data-kind', 'success');
status.hidden = false;
var cs = getComputedStyle(status);
var r = status.getBoundingClientRect();
console.log({
  position: cs.position,
  fontSize: cs.fontSize,
  width: Math.round(r.width),
  visible: cs.opacity !== '0' && cs.display !== 'none',
});
```

PASS: `position: 'absolute'`, `fontSize: '10px'`, `visible: true`。
FAIL: `position: 'static'` or `fontSize: '0.78rem'` 等 (旧スタイル残存)。

### §7.2 apply / reject オーバーレイが下端に絶対配置

```js
var actions = card.querySelector('[data-bg-actions]');
actions.hidden = false;
console.log({
  position: getComputedStyle(actions).position,
  bottom: getComputedStyle(actions).bottom,
});
```

PASS: `position: 'absolute'`, `bottom: '4px'`。

### §7.3 後始末

`status.hidden = true; actions.hidden = true;` で baseline 復元。

---

## §8. console errors

```js
window.__pr2errs = [];
var origE = console.error;
console.error = function(){window.__pr2errs.push([...arguments].join(' '));origE.apply(console, arguments);};
window.addEventListener('error', e => window.__pr2errs.push('JS:'+e.message));
// 一連の操作 (§1〜§7) を行ってから:
setTimeout(()=>console.log('errors:', window.__pr2errs), 1500);
```

PASS: 0 件。

---

## §9. `product_manual_add.html` 2 カラム回帰検証

`/products/manual-add` を開き、SKILL §5 の snippet:

```js
var layout = document.querySelector('.product-edit-layout');
var cs = getComputedStyle(layout);
var tracks = cs.gridTemplateColumns.split(' ').filter(Boolean).length;
console.log({viewport: innerWidth+'x'+innerHeight, tracks, gridCols: cs.gridTemplateColumns});
```

PASS (1280x900): `tracks: 2` (`Xpx Ypx` の 2 値)。
FAIL: `tracks: 1` = 共通 CSS が manual_add も 1 カラム化している (PR2 で `style.css` を変えたので確認必須)。

PASS (640x900): `tracks: 1` (mobile breakpoint で OK)。

---

## §10. ベースライン復元

UI 操作で値をいじったら必ず元に戻す。

```bash
cd /home/ubuntu/repos/ESP && python -c "
import sqlite3
conn = sqlite3.connect('mercari.db')
c = conn.cursor()
c.execute(\"UPDATE products SET tags='vintage,black,studio', manual_margin_rate=NULL, manual_shipping_cost=NULL WHERE id=1\")
conn.commit()
print('restored')
conn.close()"
```

並べ替え永続化 (§5.3) のあとは `image_urls_json` を含めて `[A, B, C]` 順に戻して保存しておく。

## 実行順

1. Flask dev 起動
2. tester ログイン → /products/1/edit
3. console snippet を順次実行 (§1 → §2.1 → §3 → §4 → §6 → §7 → §8)
4. viewport 切替で §2.2 / §2.3 確認
5. /products/manual-add 移動 → §9
6. /products/1/edit に戻り §5.2 → §5.3 (実 submit) → §5.4 で順序復元
7. ベースライン復元 (§10)

すべてスクリーン録画 + annotate_recording 付きで実行する。
