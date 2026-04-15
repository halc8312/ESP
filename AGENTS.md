# ESP Agent Guide

This repository is primarily edited by AI coding agents such as Claude Code and Codex.

## Scope

- Work in the ESP app unless the user explicitly asks otherwise.
- Do not edit `llama.cpp/` unless the request is specifically about that subtree.

## Current deployment contract

- Current production shape is `single-web + SCRAPE_QUEUE_BACKEND=inmemory`.
- Current live Render topology is one Web Service; there is no dedicated background worker in that path.
- `render.yaml` describes a future split deployment (`web + worker + postgres + key value`) but it is not the default production path.
- In that future split, `esp-web` is the web service, `esp-worker` is the background worker, `esp-keyvalue` is Redis, and `esp-postgres` is PostgreSQL.
- `worker.py` is the dedicated RQ/split-worker entrypoint.

## High-risk invariants

- Never expose `source_url`, `site`, or other internal sourcing details in the public catalog.
- Preserve user/shop/pricelist isolation.
- Do not silently convert current single-web assumptions into RQ/worker assumptions.
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

# current production (single-web) safety gate
flask single-web-redeploy-readiness

# future split-render safety gate
flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict
```

## Current feature reality

- Implemented: compacted list/edit/extract UI, public pricelist layouts, Quick View, search, theme persistence, product image upload.
- Not yet implemented or still pending specification: translation workflow, image background removal, pricelist category filter, PayPal/simple EC.

## More context

- Start with `README.md`.
- For operator workflows, read `docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md` and `docs/RENDER_CUTOVER_RUNBOOK.md`.
