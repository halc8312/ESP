# Test plan – PR #96 (non-PayPal UI)

Scope = 4 features changed in this PR. Adversarial: every step has a concrete pass/fail value such that a broken implementation would produce a different observable.

Server: `http://localhost:5000` (already running).
User: `tester / TestPass123!` (1 shop, 3 products, 1 pricelist).
Catalog public token: `769c196f49ad572a0733bf955a1acdcf` (layout currently `grid` — switch to `list` via pricelist edit before catalog tests).

DB baseline (pre-test snapshot):
- Pricing rule 1: margin=30%, shipping=500, fee=100
- P1: id=1, rule_id=1, last_price=2000, selling_price=3350, tags=`vintage,camera,black`, manual_margin=NULL, manual_shipping=NULL
- P2: id=2, rule_id=1, last_price=2500, selling_price=4000, manual=NULL/NULL
- P3: id=3, rule_id=1, last_price=3000, selling_price=4650, manual=NULL/NULL

---

## Test 1 — Catalog `list` layout (5-column high-density rows)

**Setup**: PriceList layout currently is `grid`. Switch to `list` first via owner UI: `/pricelist/<id>/edit`, change `layout` to `list`, save. Then visit `/catalog/769c196f49ad572a0733bf955a1acdcf?layout=list`.

### 1.1 Desktop (≥1000px)
- Maximize browser ⇒ window width ≥ 1280px.
- **Assertion**: each `.product-card` element renders in a single horizontal row with 5 inline regions visible: thumbnail (≤80px wide), title+tags block, price, stock, action button. Verifiable via `getComputedStyle(card).display === "grid"` AND `gridTemplateColumns` returns 5 tracks.
- **Fail criteria**: `display !== "grid"` OR `gridTemplateColumns` has fewer than 5 tracks OR thumbnail > 100px wide OR row wraps to multiple visible lines.

### 1.2 Tablet breakpoint (≤900px, >640px)
- Resize viewport to 800×900 (DevTools device emulation).
- **Assertion**: card collapses to a 3-column or wrap layout with thumbnail + title block on first row, price/stock/action wrap onto a second row. Title remains readable, no horizontal scroll on the card itself.
- **Fail criteria**: card still uses 5-column grid AND text overflows OR card collapses but action button is invisible/clipped.

### 1.3 Mobile breakpoint (≤640px)
- Resize viewport to 375×800.
- **Assertion**: card stacks vertically (`grid-template-columns: 1fr` or each region on its own row). Thumbnail visible, title visible, price visible, action button visible.
- **Fail criteria**: any of those 4 elements not visible OR overflow.

### 1.4 Tag pill styling on the card
- On desktop list view, locate P1 card.
- **Assertion**: tag chips have a visible 1px border and neutral background (rgba(255,255,255,0.05) on dark theme) instead of the original colored pill background.
- **Fail criteria**: chips render with the previous bright colored pill background.

### 1.5 Other layouts not regressed
- Switch pricelist back to `grid`, reload `/catalog/<token>`.
- **Assertion**: cards render as the existing image-card grid (≥3 cards across at desktop), layout unchanged from pre-PR.
- **Fail criteria**: grid layout broken, cards stretched full-width, or card structure changed.

---

## Test 2 — Tag pill input on `/product/<id>` edit page

Path: `/product/1`. P1 starts with `tags="vintage,camera,black"`.

### 2.1 Initial render = 3 pills
- Open `/product/1`.
- **Assertion**: 3 pills rendered (`vintage`, `camera`, `black`), each with an `×` remove button. Hidden `<input name="tags">` value = `"vintage,camera,black"`.
- **Fail criteria**: any pill missing OR hidden input value differs.

### 2.2 Add via Enter
- Type `studio` into the tag entry field, press Enter.
- **Assertion**: 4th pill `studio` appears AND hidden input becomes `vintage,camera,black,studio`.
- **Fail criteria**: pill not added OR hidden input not updated OR form submitted (Enter must NOT submit form).

### 2.3 Add via comma
- Type `mint,` into the tag entry field.
- **Assertion**: 5th pill `mint` appears, hidden input ends with `…,studio,mint`. Trailing comma is consumed (not left in entry field as raw text).
- **Fail criteria**: pill not created OR comma persists in the entry field as text.

### 2.4 Backspace deletes last pill on empty input
- With entry field empty, press Backspace once.
- **Assertion**: `mint` pill removed, hidden input loses `,mint`.
- **Fail criteria**: nothing removed OR more than one pill removed.

### 2.5 Click × to remove a non-tail pill
- Click `×` on the `camera` pill.
- **Assertion**: `camera` removed, hidden input becomes `vintage,black,studio`.
- **Fail criteria**: wrong pill removed OR hidden input not synced.

### 2.6 Duplicate prevention (case-insensitive)
- Type `VINTAGE`, press Enter.
- **Assertion**: no new pill added (already `vintage` exists, case-insensitive). Hidden input unchanged.
- **Fail criteria**: a duplicate `VINTAGE` pill appears.

### 2.7 Persistence
- Save the form. Reload `/product/1`.
- **Assertion**: pills now show `vintage`, `black`, `studio` in that order. SQL `SELECT tags FROM products WHERE id=1` returns `vintage,black,studio`.
- **Fail criteria**: tags not persisted OR persisted as something else (e.g. with embedded spaces, wrong order, or empty).

---

## Test 3 — Image lightbox on `/product/<id>` edit page

Need at least one product image. If P1 has none, attach 2 images first via the existing image upload UI (this is setup, not a test).

### 3.1 Click thumbnail opens modal
- Click on a product image thumbnail.
- **Assertion**: a fixed-position modal overlay covers the viewport, the clicked image is shown enlarged.
- **Fail criteria**: no modal opens OR underlying form receives a click (e.g., delete fires).

### 3.2 ESC closes modal
- Press ESC.
- **Assertion**: modal disappears, focus returns to page.
- **Fail criteria**: modal still open.

### 3.3 Background click closes modal
- Re-open by clicking thumbnail. Click on the dark backdrop area (not the image).
- **Assertion**: modal closes.
- **Fail criteria**: modal stays open OR clicking image area closes it.

### 3.4 × button closes modal
- Re-open. Click the `×` close button inside the modal.
- **Assertion**: modal closes.
- **Fail criteria**: modal stays open.

### 3.5 Arrow navigation wraps
- Re-open, then press `→`.
- **Assertion**: 2nd image now displayed.
- Press `→` again with only 2 images.
- **Assertion**: wraps back to 1st image.
- Press `←`.
- **Assertion**: returns to 2nd image (i.e. wraps backward from 1st).
- **Fail criteria**: navigation does not advance OR does not wrap.

### 3.6 Action buttons are NOT triggered when present
- Find an image-card with delete / "白抜き" / drag-handle buttons. Click the delete button.
- **Assertion**: confirmation dialog (or delete action) fires; **lightbox does NOT open** for this click.
- **Fail criteria**: lightbox opens AND delete also fires (event bubbling not stopped).

---

## Test 4 — Manual price override (UI + API + persistence)

P1 (`/product/1`): `last_price=2000`, rule (margin=30, ship=500, fee=100) currently produces selling_price=3350.

### 4.1 Toggle reveals fields
- Open `/product/1`. Locate the "個別の利益率/送料" section.
- **Assertion**: the toggle/checkbox is OFF initially. The margin% and shipping¥ inputs are hidden or disabled.
- Click the toggle ON.
- **Assertion**: margin% input and shipping¥ input become visible/enabled. "再計算して価格に反映" button visible.
- **Fail criteria**: toggle missing, fields stay hidden, or button missing.

### 4.2 Recalc API – with manual margin only
- Enter margin=50, leave shipping blank. Click "再計算".
- **Assertion**: client makes `POST /api/products/1/recalc-price` with body `{"manual_margin_rate":50,"manual_shipping_cost":null}`. Response = HTTP 200 JSON with `selling_price = (2000+500)*1.50+100 = 3850`.
- **Assertion**: variant price input(s) on the page get updated to `3850`.
- **Fail criteria**: request goes to `/api/product/1/...` (singular old path) OR response selling_price ≠ 3850 OR variant inputs not updated.

### 4.3 Recalc API – with both overrides
- Set margin=40, shipping=300. Click "再計算".
- **Assertion**: response selling_price = `(2000+300)*1.40+100 = 3320`. Variant inputs updated to 3320.
- **Fail criteria**: response value ≠ 3320 OR formula appears wrong (e.g. ignores fixed_fee=100).

### 4.4 Recalc with last_price=0 / missing → 400
- (Hypothetical / via direct curl) Send the same recalc against a product with `last_price=NULL`. Expect HTTP 400 with `仕入価格が未取得` message. SKIP if no such product exists in test DB; mark as untested rather than risking false-pass.

### 4.5 Persistence on save (override ENABLED)
- With margin=40, shipping=300 set, click form save.
- Reload `/product/1`.
- **Assertion**: toggle is still ON, margin field shows `40`, shipping field shows `300`. SQL `SELECT manual_margin_rate, manual_shipping_cost FROM products WHERE id=1` returns `(40, 300)`.
- **Fail criteria**: any of those values reverted to NULL or different value.

### 4.6 Persistence on save (override DISABLED)
- Toggle OFF, save.
- Reload `/product/1`.
- **Assertion**: toggle OFF. SQL returns `(NULL, NULL)`.
- **Fail criteria**: values not cleared.

### 4.7 Background recalc gating (regression for Devin Review issue #5)
- Set P1 manual_margin_rate=NULL, manual_shipping_cost=NULL, pricing_rule_id=NULL via SQL. Set manual_margin_rate=20.
- Trigger recalc by changing `last_price` from 2000 → 1000 (simulate scrape) and call `update_product_selling_price(1)` directly via Python repl (no rule, but manual_margin set).
- **Assertion**: function returns True, `selling_price` becomes `(1000+0)*1.20+0 = 1200` (no rule → fee=0, ship=0).
- **Fail criteria**: returns False (regression — manual-override-only product silently skipped) OR selling_price not updated.
- This test directly verifies the helper `product_has_pricing_config()` is honored end-to-end.

### 4.8 Restore baseline
- Restore P1 to original state via SQL: `pricing_rule_id=1, last_price=2000, manual_margin_rate=NULL, manual_shipping_cost=NULL, selling_price=3350`. (Cleanup, not an assertion.)

---

## Notes

- All UI tests recorded in browser; annotations added at each test_start / assertion.
- Shell-only tests (4.7, 4.8) executed via Python REPL, captured as text in test report — not recorded.
- If any single assertion fails, report it as failure; do NOT declare overall success.
- Keep grid/editorial layout untouched (no Test 1 modifications to those layouts in this PR).
