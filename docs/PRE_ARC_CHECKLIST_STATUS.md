# Pre-Arc Checklist Status

- Updated: 2026-03-18
- Scope: local, no-additional-cost checklist hardening before `Arc 1 / Move A1`

## Automated now

- API polling contract snapshots are fixed in:
  - `tests/fixtures/pre_arc/api_scrape_status_completed.json`
  - `tests/fixtures/pre_arc/api_scrape_jobs_preview.json`
- Export goldens are fixed in:
  - `tests/fixtures/pre_arc/export_shopify_single.csv`
  - `tests/fixtures/pre_arc/export_ebay_single.csv`
- Regression coverage for those fixtures lives in:
  - `tests/test_pre_arc_checklist.py`
- Existing persistence contract anchors for `save_scraped_items_to_db()` remain in:
  - `tests/test_product_service.py`

## Covered checklist items

- current API JSON snapshots for `/api/scrape/status/<job_id>` and `/api/scrape/jobs`
- golden outputs for Shopify and eBay export paths
- contract test anchor around `save_scraped_items_to_db()`

## A1 local acceptance coverage

The following `Arc 1 / Move A1` checks are now covered locally without external traffic:

- async fan-out helper preserves input ordering even when completions finish out of order
- bounded concurrency is enforced by test
- partial failure returns surviving items without collapsing the whole batch
- sync wrappers can execute safely even when a loop is already running in the current process
- site-level async settings are configurable by environment variable and include conservative Mercari defaults

Relevant tests:

- `tests/test_scraping_client_async.py`
- `tests/test_scraping_logic.py`
- `tests/test_search_result_count_guarantees.py`

## A2 local verification coverage

The following `Arc 1 / Move A2` checks are now covered locally without changing the default production path:

- extractor priority/provenance is fixed for Yahoo, Yahuoku, Offmall, SNKRDUNK, and Surugaya
- invalid-primary fallthrough is centralized in extraction policy helpers
- `_scrape_meta` remains internal and does not leak into the persistence contract
- Mercari network payload support is staged behind feature flags:
  - `MERCARI_CAPTURE_NETWORK_PAYLOAD=true` enables capture + shadow compare while keeping DOM as the returned result
  - `MERCARI_USE_NETWORK_PAYLOAD=true` enables payload-first field selection with DOM fallback preserved per field
- Mercari capture failure does not fail the scrape result when the DOM path still succeeds
- Mercari can fall back to captured payload only when DOM fetch fails and payload-first mode is explicitly enabled

Relevant tests:

- `tests/test_extraction_priority.py`
- `tests/test_mercari_network_payload.py`

Mercari flag precedence is currently:

- both flags false -> DOM only
- `MERCARI_CAPTURE_NETWORK_PAYLOAD=true`, `MERCARI_USE_NETWORK_PAYLOAD=false` -> capture + shadow compare only
- `MERCARI_USE_NETWORK_PAYLOAD=true` -> payload-first with field-level DOM fallback, and capture is implied automatically

## A3 local verification coverage

The following `Arc 1 / Move A3` checks are now covered locally without sending real webhook traffic:

- silent selector alerts are emitted for full healing failure
- silent selector alerts are emitted when healing succeeds in memory but selector persistence fails
- repeated low-confidence healing triggers a warning-level selector alert
- alert dispatch failures do not raise back into scrape execution

Relevant tests:

- `tests/test_selector_healer.py`

## Arc 1 closeout tooling

- low-cost KPI harness:
  - `scripts/arc1_kpi_probe.py`
- run instructions and closeout matrix:
  - `docs/ARC1_KPI_RUNBOOK.md`

## Known local warnings

- `services/product_service.py` still emits `datetime.utcnow()` deprecation warnings during some test runs
- this is tracked as non-blocking debt for `Arc 1`, but should be cleaned up before or during `Arc 2`

## Intentionally deferred

- Live-network throughput baseline on target sites
  - Deferred until explicitly approved because it depends on real scrape execution and can create external traffic/load.
- Production-schema dump and diff against the actual live database
  - Deferred because the useful artifact is environment-specific and should be captured from the target database before `Arc 2`.
- Render resource measurements
  - Deferred because they require deployment/runtime changes outside the local no-cost scope.
- Live webhook delivery to Slack or another external endpoint
  - Deferred until a real `SELECTOR_ALERT_WEBHOOK_URL` is provided and external traffic is explicitly approved.

## Local verification command

```powershell
pytest tests/test_pre_arc_checklist.py tests/test_product_service.py
```

This keeps the current work inside the no-additional-cost boundary while turning the main compatibility surfaces into explicit regression fixtures.
