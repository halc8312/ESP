# PR #96 — End-to-end test report

PR: https://github.com/halc8312/ESP/pull/96
Branch: `devin/1777324280-non-paypal-ui` @ `aa26325`
Recording: `rec-9f142b3bb02e4b0e8888260b9b107948-edited.mp4`

## Result summary

| # | Feature | Result |
|---|---|---|
| 1 | Catalog `list` 5-col layout + breakpoints + tag pill chip on card | **PASS** |
| 2 | Tag pill UI on `/product/<id>` (Enter / comma / Backspace / × / dedup / persist) | **PASS** |
| 3 | Image lightbox (open / ESC / backdrop / × / arrow wrap / action-button delegation) | **PASS** |
| 4 | Manual price override (toggle / recalc API / persist on / persist off / background recalc gating) | **PASS** |

One caveat (not a feature regression): the click on "この内容で保存" reported a Playwright timeout, but the form was submitted in both cases (verified by `保存済み` badge and DB row). Treated as benign Playwright timing artifact.

One bug **found and fixed during testing**: the recalc button JS did not include the `X-CSRFToken` header, so Flask-WTF rejected the POST with 400. Fixed in `aa26325` by reading `<meta name="csrf-token">` and adding the header to `fetch`. Test 4.2 was re-run after the fix and passed.

---

## Test 1 — catalog list layout (mock B)

Switched pricelist layout to `list`, visited `/catalog/<token>?layout=list`.

- **1.1 Desktop ≥1280px** — `display:grid` confirmed via `getComputedStyle`, 5 tracks: 80px / 1fr / auto / auto / auto. PASS.
- **1.2 Tablet ≤900px** — wraps to 2 rows; thumb+title on row 1, price/stock/action on row 2. No horizontal overflow. PASS.
- **1.3 Mobile ≤640px** — full vertical stack; all 4 regions visible. PASS.
- **1.4 Tag pill chips on card** — neutral `rgba(255,255,255,0.05)` bg + 1px border. PASS.
- **1.5 grid layout regression check** — switched back to `grid`, layout unchanged from pre-PR. PASS.

## Test 2 — tag pill input on `/product/1`

P1 starts with `tags="vintage,camera,black"`.

- **2.1** initial render = 3 pills, hidden input = `vintage,camera,black`. PASS.
- **2.2** type `studio` + Enter → 4th pill, hidden = `…,studio`, no form submit. PASS.
- **2.3** type `mint,` → 5th pill, trailing comma consumed. PASS.
- **2.4** Backspace on empty input → removes `mint`. PASS.
- **2.5** click × on `camera` → removed, hidden = `vintage,black,studio`. PASS.
- **2.6** type `VINTAGE` + Enter → no duplicate added (case-insensitive dedup). PASS.
- **2.7** save + reload → DB `tags = "vintage,black,studio"` and pills match. PASS.

## Test 3 — image lightbox on `/product/1`

P1 had 3 images.

- **3.1** click thumbnail → modal opens. PASS.
- **3.2** ESC closes. PASS.
- **3.3** backdrop click closes. PASS.
- **3.4** × button closes. PASS.
- **3.5** arrow nav wraps in both directions (1→2→1→back to 2). PASS.
- **3.6** clicking the delete `✕` button on an image card → confirm dialog fires, lightbox does **not** open (event delegation honors `closest('button')`). PASS.

## Test 4 — manual price override

Baseline: P1 last_price=2000, rule 1 (margin=30, ship=500, fee=100), selling_price=3350.

### 4.1 toggle reveals fields — PASS
Initially checkbox OFF, body has `hidden` attr. Toggling ON unhides body and reveals margin% input, shipping¥ input, "再計算して価格に反映" button.

### 4.2 recalc API — margin only — PASS *(after CSRF fix)*
margin=50, shipping blank. POST `/api/products/1/recalc-price` returned HTTP 200 with `selling_price=3850 = (2000+500)*1.50+100`. Both variant inputs updated to 3850.

Banner: `再計算結果: ¥3850 を 2 件のバリエーション欄に反映しました（保存で確定）`.

Originally failed with 400 because the fetch lacked `X-CSRFToken`; fix committed as `aa26325`.

### 4.3 recalc API — both overrides — PASS
margin=40, shipping=300 → response `selling_price=3320 = (2000+300)*1.40+100`. Variants updated to 3320.

### 4.4 recalc with last_price=NULL → 400 — UNTESTED
No product with `last_price=NULL` in the test DB. Skipped per the test plan to avoid risk of false-pass.

### 4.5 persistence on save (override ENABLED) — PASS
With margin=40, shipping=300, clicked save. After reload:
- checkbox=true, margin=40, shipping=300, body visible.
- DB: `(id=1, manual_margin_rate=40, manual_shipping_cost=300, pricing_rule_id=1, last_price=2000, selling_price=3320)`.

### 4.6 persistence on save (override DISABLED) — PASS
Toggled OFF + save. After reload:
- checkbox=false, fields hidden.
- DB: `(id=1, manual_margin_rate=NULL, manual_shipping_cost=NULL, …)`.

### 4.7 background recalc gating (Devin Review #5 regression) — PASS
SQL: set P1 `pricing_rule_id=NULL, manual_margin_rate=20, manual_shipping_cost=NULL, last_price=1000`.
Then via Python REPL inside Flask app context:

```
PRE rule= None margin= 20 ship= None last= 1000 sell= 3320
eligible= True
rc= True
POST sell= 1200    ← (1000+0) * 1.20 + 0
```

Confirms `product_has_pricing_config()` returns True for manual-override-only products and `update_product_selling_price()` recalculates them. The original Devin Review concern that manual-override-only products would be silently skipped is fixed.

### 4.8 baseline restore — done
P1/P2/P3 restored to original `(rule=1, manual=NULL/NULL, last=2000/2500/3000, sell=3350/4000/4650)`.

---

## Files / commits in this PR

- catalog list layout: `static/css/catalog.css`
- tag pill UI: `templates/product_detail.html`, `static/css/style.css`
- image lightbox: `templates/product_detail.html`, `static/css/style.css`
- manual override: `models.py`, `database.py`, `services/pricing_service.py`, `services/product_service.py`, `services/monitor_service.py`, `cli.py`, `routes/api.py`, `routes/products.py`, `templates/product_detail.html`
- helper `product_has_pricing_config()`: `services/pricing_service.py`
- CSRF fix (this session): `templates/product_detail.html` (commit `aa26325`)

CI: green at `aa26325`.
