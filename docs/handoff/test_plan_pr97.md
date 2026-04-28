# Test plan — PR #97 (商品編集 PR1: foundation refactor)

PR: https://github.com/halc8312/ESP/pull/97 (commit `c497567`)

Scope: PR is **structural / visual only** (no models, routes, or form `name=` attributes change). All adversarial checks below are concrete and will look DIFFERENT if the change is broken.

---

## 1. Sticky header is actually sticky and renders all four affordances

1.1. Open `/products/1/edit` and confirm a single `<header class="product-edit-page-header">` with **all four** elements: back-arrow icon (`.product-edit-back-icon` with text `‹`), `<h1>` showing product title, `#saveStateBadge` chip with text `保存済み`, `#heroStatusBadge` chip showing `公開中` or `下書き`, `#heroReadinessBadge` button, and `<button form="productEditForm" class="product-edit-header-save">保存する</button>`.
   - **PASS**: all 6 elements present, header has `position: sticky` (computed style) and `top: 0`.
   - **FAIL**: any element missing, or `position` not `sticky`.

1.2. Scroll the page to `window.scrollY ≥ 1500`. Re-query `getBoundingClientRect()` of the header.
   - **PASS**: `top` is between `0` and `5` (header pinned to top of viewport).
   - **FAIL**: header has scrolled off-screen (`top < -10`).

1.3. The `保存する` button (`.product-edit-header-save`) is structurally **outside** the `<form>` and uses `form="productEditForm"`. Click it and confirm form submission.
   - **PASS**: browser navigates to `POST /products/1/edit`, response is 200/302 with redirect to `/products/1/edit` (round-trip), no console error.
   - **FAIL**: button is inert (no submission), or `form` attribute is missing.

---

## 2. 保存前チェック popover (data-ready, live updates, 22px icons)

2.1. On a fully populated product, `#heroReadinessBadge` should have `data-ready="ok"` and inner `<span id="heroReadinessText">` showing `チェック 5/5`. Computed CSS background of badge: `rgb(236, 253, 243)` (the `#ecfdf3` ok color).
   - **PASS**: matches.
   - **FAIL**: data-ready missing or `todo`, count not 5/5.

2.2. Hover over the badge. A `.product-edit-checklist-popover` becomes visible (`display: block`) with **5 `<li data-check-key="…">` items**: `images`, `title`, `description`, `variants`, `price`. The popover **must still exist** (regression check for the bug fixed in 9340fab — `textContent =` previously destroyed it on page load).
   - **PASS**: 5 list items visible on hover, each contains `.product-edit-check-icon` showing `OK` or `要`.
   - **FAIL**: popover empty / missing / contains plain text instead of `<ul>`.

2.3. Measure `getBoundingClientRect()` of any `.product-edit-checklist-popover .product-edit-check-icon`.
   - **PASS**: width and height are both `22 ± 1` px (NOT 42px — regression check for 8868a33 min-width fix).
   - **FAIL**: width or height ≥ 30 px.

2.4. Live update: clear the `name=title` field (the title text input on the basic settings card, **not** the read-only source title in 仕入設定).
   - **PASS**: header `#heroReadinessText` instantly updates from e.g. `5/5` → `4/5`, `data-ready` flips to `todo`, popover `<li data-check-key="title">` flips from `is-complete` to non-complete and the icon text changes from `OK` to `要`.
   - **FAIL**: count or icon doesn't update; popover disappears after the edit (it would if the bug came back).

---

## 3. 仕入設定 (自動取得) read-only card content

Test product (id=1): site=`mercari`, last_price=2000, last_status=`on_sale`, last_title=`テスト商品 1 ヴィンテージ カメラ`, source_url=`https://example.com/item/1`.

3.1. Locate `<section id="productSourcePanel">`. It must render **above** `<div class="product-edit-layout">`.
3.2. Inside, exactly 5 `.product-source-summary-row` rows in this order: `取得元サイト`, `仕入れ価格 (記録)`, `仕入れ元の状態`, `最後に取得したタイトル`, `取得元URL`. The last 2 have class `is-wide` (full row).
3.3. Concrete values:
   - 取得元サイト: `mercari`
   - 仕入れ価格 (記録): `¥2,000`
   - 仕入れ元の状態: `on_sale`
   - 最後に取得したタイトル: `テスト商品 1 ヴィンテージ カメラ`
   - 取得元URL: `<a href="https://example.com/item/1" target="_blank">…</a>`
   - **PASS**: all 5 values match exactly.
   - **FAIL**: any wrong, missing, or shows `—` for a populated field.

3.4. Card must be marked read-only (`<span class="product-edit-summary-meta">読み取り専用</span>` in header) and contain **no editable inputs**.
   - **PASS**: zero `<input>`/`<textarea>`/`<select>` inside `#productSourcePanel`.
   - **FAIL**: any editable field exists.

3.5. **Public catalog leak check** (high-risk invariant per AGENTS.md): browse to a public catalog page that exposes product 1; the response HTML **must NOT** contain `https://example.com/item/1` or the string `mercari` outside non-public shop branding. (curl with no auth, grep response.)
   - **PASS**: `source_url` and `site` not in public HTML.
   - **FAIL**: leak detected.

---

## 4. Save round-trip persists (form name= attributes unchanged)

4.1. Edit product 1: change title to `[E2E] PR97 product`, add a new tag pill `pr97test`, set manual margin override = 25.
4.2. Click the **header** `保存する` (top of page, NOT the bottom button — this verifies `form="productEditForm"` wiring).
4.3. After redirect, reload `/products/1/edit`.
   - **PASS**: title input value = `[E2E] PR97 product`; tag pill `pr97test` present in DOM; manual margin override input value = `25`; readiness badge still `5/5`.
   - **FAIL**: any value lost, or 500 error.

4.4. Restore product 1 to original state (revert title/tag/margin) so test data is clean for future runs.

---

## 5. Regression: `<details>` accordions are gone

5.1. In the rendered DOM of `/products/1/edit`, inside `<form id="productEditForm">`, count `<details class*="product-edit-panel">` elements.
   - **PASS**: zero. All section panels are now `<section>`.
   - **FAIL**: any `<details>` still in the form (the refactor was incomplete).

5.2. The 5 expected `<section>` cards exist by id: `basicSettingsPanel`, `productImagesPanel`, `productDescriptionPanel`, `productVariantsPanel`, plus the SEO/tags panel (search by `<h2>` text containing `SEO`).
   - **PASS**: all 5 present.

5.3. The Beginner-Friendly Flow hero block must NOT exist (search for `.beginner-flow-hero` or text `初めての編集`).
   - **PASS**: not found in DOM.

---

## 6. Shared CSS regression — `product_manual_add.html` still two-column

This is the bug fixed by c497567. Critical regression check.

6.1. Open `/products/manual-add` at viewport ≥ 1024px.
6.2. Locate `<div class="product-edit-layout">` and `<div class="product-edit-side">`.
   - **PASS**: `.product-edit-layout` `getComputedStyle().gridTemplateColumns` reports **two tracks** (e.g. ~`812px 426px` or similar — NOT a single value).
   - **PASS**: `.product-edit-side` `getBoundingClientRect().left > 600` (clearly on the right side, not stacked under main).
   - **PASS**: `.product-edit-side` `position` is `sticky` (the @media (min-width: 1024px) rule applies).
   - **FAIL**: any of the three; in particular gridTemplateColumns has a single track means the layout collapsed.

6.3. Same product_manual_add page at viewport 800×900 (below the 1023px breakpoint):
   - **PASS**: gridTemplateColumns collapses to 1 track (responsive), no horizontal scrollbar, sidebar wraps below main.

---

## 7. No console errors / warnings

7.1. Open `/products/1/edit` with devtools console open. Reload.
   - **PASS**: zero `console.error` / uncaught exceptions during page load and during the edits in tests 2.4 and 4.1–4.3.
   - **FAIL**: any error referencing `summaryImageValue`, `checklistCountBadge`, `heroShopBadge`, `cannot read … of null`, or other JS errors. (Per the PR description these elements are intentionally removed and JS now no-ops via null guards — verify it actually does.)

---

## 8. Mobile (≤640px) sticky header still usable

8.1. Set viewport to 375×800 (iPhone size). Reload `/products/1/edit`.
   - **PASS**: header still renders with back-arrow + at least readiness badge + 保存する; header padding shrinks (`10px 12px` per the @media rule); save button text reads `保存する` and is tappable (height ≥ 32px).
   - **FAIL**: header overflows horizontally, save button covered, or back-arrow missing.

---

## 9. Status chip color matches product.status

9.1. With product 1 set to `status = active`, `#heroStatusBadge` has class `is-active` and visible label is `公開中`.
9.2. Edit product 1 to `status = draft` via the basic settings panel, save, reload.
   - **PASS**: `#heroStatusBadge` class flips to `is-draft`, label is `下書き`. Restore to original after.
   - **FAIL**: chip class/label doesn't change.

---

## Notes / known untestable

- **PR2/3/4/5 features are explicitly out of scope** (image 10-col grid, variant disclosure, English title sync, right sidebar). Do not assert on these.
- The `<button>保存する</button>` outside `<form>` relies on the HTML5 `form` attribute. All modern browsers (incl. Playwright/Chrome) support this — no IE compatibility test needed.

## Test data prep

- Login as `tester` (password reset to known value via `python -c …` before recording).
- Confirm product 1 has source_url before recording.
- After all tests, restore product 1 (title/tag/margin/status) to original.
