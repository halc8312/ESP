# ESP Agent Guide

Project-specific guidance for contributors and coding assistants.

## Scope

- Work in the ESP app unless the user explicitly asks otherwise.
- Do not edit `llama.cpp/` unless the request is specifically about that subtree.

## Current deployment contract

- Current live Render topology includes `esp-web`, `esp-worker`, `esp-keyvalue`, and `esp-postgres`.
- Do not assume production is single-web only. Treat single-web commands and runbooks as compatibility tooling unless the user explicitly says otherwise.
- `render.yaml` should stay aligned with the live split deployment contract.
- In the split topology, `esp-web` is the web service, `esp-worker` is the background worker, `esp-keyvalue` is Redis/Valkey, and `esp-postgres` is PostgreSQL.
- `worker.py` is the dedicated RQ/split-worker entrypoint.

## High-risk invariants

- Never expose `source_url`, `site`, or other internal sourcing details in the public catalog.
- Preserve user/shop/pricelist isolation.
- Do not silently change the web/worker/database/queue contract on Render.
- In production, `SECRET_KEY` must be explicitly set and shared between `esp-web` and `esp-worker`.

## Key files

- Product list: `routes/main.py`, `routes/api.py`, `templates/index.html`
- Product edit: `routes/products.py`, `templates/product_detail.html`
- Scrape UI: `routes/scrape.py`, `templates/scrape_form.html`, `static/js/scrape_form.js`
- Pricelist admin: `routes/pricelist.py`, `templates/pricelist_edit.html`
- Public catalog: `routes/catalog.py`, `templates/catalog.html`
- Worker/runtime: `worker.py`, `services/worker_runtime.py`, `app.py`, `render.yaml`
- Main E2E coverage: `tests/test_e2e_routes.py`, `tests/test_worker_runtime.py`, `tests/test_worker_entrypoint.py`

## Recommended verification

```bash
# UI / route changes
pytest tests/test_e2e_routes.py -q

# worker / runtime changes
pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q

# legacy single-web compatibility gate
flask single-web-redeploy-readiness

# current split-render safety gate
flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict
```

## Current feature reality

- Implemented: compacted list/edit/extract UI, public pricelist layouts, Quick View, search, theme persistence, shop-bound logo display, product image upload, search-URL extraction (auto-detect item vs search URL), preview filtering (× exclude) with pricelist-only registration (`is_listed=False`), translation workflow (auto-translate on registration with `auto_apply`, manual review/apply, Argos/OpenAI backends), default pricing rule on registration.
- Not yet implemented or still pending specification: PayPal/simple EC; verify category-filter scope against the current UI before promising it. Background removal has an implemented route/backend, but its production configuration and output still need environment-specific verification.

## More context

- Start with `README.md`.
- For operator workflows, start with `docs/README.md` and `docs/OPERATIONS_HANDOVER.md`. `docs/RENDER_CUTOVER_RUNBOOK.md` covers the split deployment checks; `docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md` is for legacy single-web compatibility checks.

- For module responsibilities and validation, use `docs/ARCHITECTURE.md`. Track delivery gaps in `docs/DELIVERY_STATUS.md`.
- Run migration-writing checks against an isolated database first; do not run them against production as a read-only audit.
