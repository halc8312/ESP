# ESP Backend Renewal Execution Plan

- Updated: 2026-03-18
- Status: Proposal / awaiting approval
- Scope: Backend optimization, queue/database separation preparation, distributed worker completion

## 1. Objective

Keep the current feature set intact while rebuilding the backend so it can:

1. Maximize scraping throughput on the current Render Standard setup.
2. Remove the blockers that prevent Web/Worker separation.
3. Finish with a Redis + PostgreSQL + dedicated worker architecture on Render.

This plan must preserve:

- Shopify CSV export
- eBay CSV export
- Admin UI behavior
- Existing product, snapshot, and variant data contract

## 2. Non-Negotiables

- Work proceeds strictly in order from `Arc 1` to `Arc 3`.
- No change may break `Product`, `Variant`, and `ProductSnapshot` compatibility used by export and admin flows.
- Queue/status APIs must remain usable by the current frontend while internals are replaced.
- Current scraping functionality must remain available during migration through compatibility layers and feature flags where needed.

## 3. Terminology

To avoid collision with existing project vocabulary, this document uses:

- `Arc`: a major execution layer
- `Move`: a concrete implementation unit inside an Arc

## 4. Current Architecture Findings

### 4.1 Database layer

Current state:

- `database.py` reads `DATABASE_URL`, but startup still relies on `Base.metadata.create_all(engine)`.
- `app.py` runs manual `ALTER TABLE` logic at startup via `run_migrations()`.
- Tests are still SQLite-file based.

Relevant files:

- `database.py`
- `app.py`
- `models.py`
- `tests/conftest.py`

Implication:

- PostgreSQL switching is not blocked by configuration, but it is blocked by schema management maturity.
- Alembic must be introduced before database backend switching can be considered safe.

### 4.2 Queue layer

Current state:

- `services/scrape_queue.py` stores all job state in process memory.
- `routes/scrape.py` enqueues jobs directly into that in-memory singleton.
- `routes/api.py` returns `/api/scrape/status/<job_id>` and `/api/scrape/jobs` from the same in-memory store.
- `Dockerfile` is pinned to `gunicorn --workers 1 --max-requests 0` specifically because of this design.

Relevant files:

- `services/scrape_queue.py`
- `routes/scrape.py`
- `routes/api.py`
- `Dockerfile`

Implication:

- Web scaling is blocked.
- Worker restart safety is blocked.
- Web/Worker separation is impossible until job state and execution move out of process memory.

### 4.3 Scraping layer

Current state:

- Mercari still pays browser startup cost in the main scrape flow and patrol flow.
- Rakuma search uses Playwright, then fetches item details over HTTP, but the detail fan-out is still effectively sequential.
- Yahoo, Yahuoku, Offmall, Surugaya, and SNKRDUNK already contain partial structured-data extraction.
- `SelectorHealer` can self-heal and persist selectors, but it does not notify external systems when healing fails.

Relevant files:

- `mercari_db.py`
- `rakuma_db.py`
- `yahoo_db.py`
- `yahuoku_db.py`
- `offmall_db.py`
- `surugaya_db.py`
- `snkrdunk_db.py`
- `services/mercari_item_parser.py`
- `services/selector_healer.py`
- `services/patrol/mercari_patrol.py`

Implication:

- The project already has the beginnings of a DOM-light strategy.
- The biggest throughput gain will come from: async fetch fan-out, reducing browser launches, and preferring data payloads over selectors.

### 4.4 UI and export dependency surface

Current state:

- The admin edit flow reads and writes `Product`, `Variant`, and latest `ProductSnapshot`.
- CSV export paths query the same tables directly.
- `save_scraped_items_to_db()` is the central persistence contract for scraped items.

Relevant files:

- `services/product_service.py`
- `routes/products.py`
- `routes/export.py`

Implication:

- Internal infrastructure can be replaced, but the persistence contract must remain stable throughout the migration.

## 5. Architecture Decisions

### 5.1 Queue technology choice

Selected for `Arc 2`: `RQ`, with an explicit execution-model decision gate before `Arc 3`.

Reason:

- Lower integration surface than Celery for this codebase.
- Easier mapping from current `job_id` polling model.
- Simpler Render deployment path for a Flask app with explicit worker separation.

Required clarification before `Arc 3`:

- Validate whether the chosen RQ worker mode can safely support long-lived browser ownership.
- If the default RQ execution model is incompatible with browser reuse, keep RQ for queue/state and execute scraping inside a worker-local executor, `SimpleWorker`, or equivalent custom runtime.
- Treat browser-runtime ownership as a separate decision from queue technology so that job APIs and queue durability are not tied to one worker implementation assumption.

Not selected: `Celery`

Reason:

- More moving parts than required for the current migration path.
- Higher operational complexity for a project that already has a clear job polling contract.

### 5.2 Browser runtime strategy

Selected:

- Keep browser startup out of Web requests.
- Move browser ownership into dedicated workers.
- Reuse one long-lived Playwright browser process per worker.
- Create and dispose only browser contexts/pages per scrape unit.

Reason:

- This is the only path that removes the repeated startup penalty while preserving site compatibility.

### 5.3 Durable job state split

Durable state is intentionally split across Redis and PostgreSQL.

Redis responsibilities:

- queue dispatch
- short-lived live progress / heartbeat
- worker lease / transient execution metadata
- retry and timeout coordination

PostgreSQL responsibilities:

- durable `scrape_jobs` record
- optional `scrape_job_events` audit trail
- tracker-visible history
- result summary
- error payload
- preview payload or payload pointer
- completed / failed job source of truth for API responses

Initial `scrape_jobs` contract:

- `job_id`
- `status`
- `site`
- `mode`
- `requested_by`
- `request_payload`
- `progress_current`
- `progress_total`
- `result_summary`
- `error_message`
- `started_at`
- `finished_at`
- `created_at`

### 5.4 Persistence idempotency and concurrency semantics

Before distributed execution is enabled, the following rules must be defined and implemented:

- natural key and uniqueness strategy for scraped source items
- explicit idempotency rule for `save_scraped_items_to_db()`
- `Product` upsert policy for repeated scrape / retry / patrol overlap
- `ProductSnapshot` append rule, including duplicate snapshot suppression
- retry behavior after partial persistence
- transaction boundary and locking / conflict policy for concurrent writers

Target direction:

- `Product` identity remains stable by normalized source item identity per user
- retries are safe to replay
- snapshot growth is meaningful rather than duplicate noise

### 5.5 Observability and KPI policy

No Arc is considered complete without measurable improvement or parity.

Required baseline and ongoing metrics:

- site-level items/min
- job duration p50 / p95
- queue wait time
- browser launch count per job
- worker/browser restart count
- memory usage
- timeout / failure rate
- selector heal success / failure count

## 6. Pre-Arc Checklist

Before `Arc 1` starts, capture the following artifacts:

- current API JSON snapshots for `/api/scrape/status/<job_id>` and `/api/scrape/jobs`
- contract tests around `save_scraped_items_to_db()`
- golden outputs for Shopify and eBay export paths
- throughput baseline for major scrape paths
- current schema dump and schema diff against `models.py`

These artifacts become the regression and success baseline for the rest of the plan.

## 7. Execution Order

The work is intentionally sequential:

1. `Arc 1: Async Core`
2. `Arc 2: State Split`
3. `Arc 3: Worker Fabric`

`Arc 2` does not begin until `Arc 1` is verified.
`Arc 3` does not begin until `Arc 2` is verified.

## 8. Arc 1: Async Core

### 8.1 Goal

Increase throughput on the current infrastructure without changing the external deployment model yet.

### 8.2 Exit Criteria

- Static/detail fetches that can be async are converted to true async fan-out.
- DOM-first extraction is reduced in favor of structured payloads.
- Selector healing failures emit silent external alerts.
- Existing scrape pages, result pages, and exports still work.
- KPI targets are evaluated against the `Pre-Arc Checklist` baseline.

### 8.3 Moves

#### Move A1: Async fetch foundation

Target files:

- `services/scraping_client.py`
- `mercari_db.py`
- `rakuma_db.py`
- `snkrdunk_db.py`

Concrete changes:

- Introduce async HTTP fetch helpers using a reusable async client/session model.
- Replace sequential item-detail loops with `asyncio.gather(..., return_exceptions=True)` plus semaphore-based concurrency control.
- Keep synchronous wrapper functions for compatibility with current route and CLI call sites.
- Preserve item ordering while allowing partial success.
- Add site-level concurrency, timeout, retry, and backoff configuration.
- Replace avoidable fixed sleeps with event-based waits or jittered pacing where anti-bot stability requires timing.

Expected effect:

- Faster search-result expansion.
- Better throughput under the current single-web-process constraint.

#### Move A2: DOM-light extraction strategy

Target files:

- `mercari_db.py`
- `services/mercari_item_parser.py`
- `yahoo_db.py`
- `yahuoku_db.py`
- `offmall_db.py`
- `surugaya_db.py`
- `snkrdunk_db.py`

Concrete changes:

- Normalize extractor priority to:
  1. captured API/network payload
  2. `__NEXT_DATA__` or JSON-LD
  3. meta tags
  4. CSS selector fallback
- For Mercari search/detail, add Playwright network interception where payload extraction is possible.
- Keep DOM fallback paths as safety nets rather than primary data sources.

Expected effect:

- Reduced fragility against frontend class churn.
- Lower selector dependency and lower healing frequency.

#### Move A3: Selector failure alerting

Target files:

- `services/selector_healer.py`
- `services/alerts.py` (new)
- optional config wiring in `app.py` or environment loading entrypoints

Concrete changes:

- Add a notifier abstraction for silent operational alerts.
- Send Slack/webhook notifications when:
  - healing fails entirely
  - healing succeeds in-memory but selector persistence fails
  - repeated low-confidence extraction is detected
- Use environment-driven configuration such as `SELECTOR_ALERT_WEBHOOK_URL`.
- Add alert dedupe, rate limit, cooldown, and severity handling so one site outage does not create alert storms.

Expected effect:

- Broken selectors stop failing silently.
- Maintenance becomes operationally visible without disturbing users.

#### Move A4: Arc 1 regression safety

Target files:

- `tests/test_selector_healer.py`
- `tests/test_scrape_preview_flow.py`
- site-specific scrape tests
- new async scraping tests as needed

Concrete changes:

- Add tests for async fan-out behavior.
- Add tests for alert dispatch on healing failure.
- Confirm preview flow, persisted flow, and result polling remain unchanged externally.
- Confirm exports still work against unchanged persistence models.

## 9. Arc 2: State Split

### 9.1 Goal

Remove the architectural blockers that prevent Render Web and Worker separation.

### 9.2 Exit Criteria

- Database schema is migration-driven.
- Queue execution no longer depends on in-process memory.
- Status APIs return durable job state.
- The app can run with PostgreSQL by `DATABASE_URL`.
- Redis and PostgreSQL responsibilities for job state are explicitly separated.

### 9.3 Moves

#### Move B1: App bootstrap separation

Target files:

- `app.py`
- `database.py`
- `cli.py`
- `tests/conftest.py`

Concrete changes:

- Refactor toward an application factory shape so Web, Worker, CLI, and tests can initialize the app differently.
- Remove import-time side effects for scheduler startup and schema mutation.
- Make database/session initialization explicit per runtime role.

Expected effect:

- Cleaner separation of concerns.
- Safe initialization for multiple process types.

#### Move B2: Durable job state contract

Target files:

- `models.py`
- `routes/api.py`
- `routes/scrape.py`
- `services/queue_backend.py` (new)
- optional `scrape_job_events` model / table

Concrete changes:

- Define status enum, progress semantics, result-summary shape, error payload shape, and retention policy.
- Add durable `scrape_jobs` storage needed by tracker UI, polling APIs, and preview flows.
- Decide whether preview payload is stored directly in PostgreSQL or referenced indirectly, but keep completed-job API reads durable.
- Keep Redis usage limited to queueing and transient execution state rather than long-term UI history.

Expected effect:

- Frontend compatibility has a durable backing model before queue cutover.
- Job APIs no longer depend on ephemeral worker memory semantics.

#### Move B3: Alembic introduction

Target files:

- `alembic.ini` (new)
- `alembic/env.py` (new)
- `alembic/versions/*` (new)
- `models.py`
- `database.py`

Concrete changes:

- Create a baseline migration from the actual live schema, not only from ORM assumptions.
- Capture schema diff before generating the baseline.
- Remove startup schema drift logic from `app.py`.
- Ensure SQLite and PostgreSQL both work under the migration system during the transition period.
- Add migration execution to CI and staging verification.

Expected effect:

- Safe schema evolution.
- Reliable PostgreSQL readiness.

#### Move B4: External job backend with RQ

Target files:

- `services/queue_backend.py` (new)
- `jobs/scrape_tasks.py` (new)
- `routes/scrape.py`
- `routes/api.py`
- `services/scrape_queue.py`
- optional new job-status persistence model/table

Concrete changes:

- Introduce Redis-backed enqueue/dequeue via RQ.
- Move scrape execution into worker-callable task functions.
- Select and document the worker execution model needed for future browser reuse.
- Persist enough job metadata and result summary to support current UI polling and tracker behavior.
- Preserve current route contracts and JSON shape as much as possible.
- Keep the queue/state layer independent from browser-runtime ownership so `Arc 3` can evolve without redoing the API contract.

Expected effect:

- Web requests stop owning job execution.
- Multi-process and multi-container operation becomes possible.

#### Move B5: Compatibility bridge and controlled cutover

Target files:

- `services/scrape_queue.py`
- `routes/scrape.py`
- `routes/api.py`
- configuration wiring

Concrete changes:

- Keep an adapter that can route to `inmemory` or `rq` via feature flag.
- Allow staged migration in dev/test/prod without changing frontend behavior.
- Update `/api/scrape/status/<job_id>` and `/api/scrape/jobs` to read durable state.

Expected effect:

- Lower-risk rollout.
- Easier rollback during cutover.

## 10. Arc 3: Worker Fabric

### 10.1 Goal

Finish the distributed worker architecture and remove repeated browser startup overhead.

### 10.2 Exit Criteria

- Dedicated worker process exists.
- Browser runtime is long-lived inside worker.
- Web no longer launches browsers for scrape execution.
- Scheduler responsibilities are no longer tied to the Web process.
- Worker execution mode, browser lifecycle, and memory ceiling are documented and verified together.

### 10.3 Moves

#### Move C1: Dedicated worker entrypoint

Target files:

- `worker.py` (new)
- `jobs/scrape_tasks.py`
- runtime configuration files

Concrete changes:

- Create a dedicated worker entrypoint for RQ.
- Initialize queue consumers and worker-only resources there.
- Separate Web-serving responsibilities from scrape execution responsibilities.

Expected effect:

- Clear Render process separation.

#### Move C2: Persistent Playwright browser pool

Target files:

- `services/browser_pool.py` (new)
- `services/browser_runtime.py` (new)
- `mercari_db.py`
- `snkrdunk_db.py`
- `services/patrol/mercari_patrol.py`

Concrete changes:

- Start a long-lived Playwright browser when the worker starts.
- Reuse that browser across jobs.
- Create/dispose contexts or pages per job rather than relaunching the browser.
- Make scraper functions accept runtime/browser injection where needed.
- Define max contexts/pages per worker, browser restart policy, crash recovery, and cleanup guarantees.
- Instrument browser pool health and restart counts.

Expected effect:

- Major reduction in browser startup overhead.
- Better high-load parallel behavior.

#### Move C3: Scheduler relocation

Target files:

- `app.py`
- new worker or cron-specific runtime files
- `services/monitor_service.py`

Concrete changes:

- Remove scheduler ownership from the Web runtime.
- Move patrol and trash-purge execution to worker-side scheduling or dedicated cron execution.
- Prevent duplicate scheduling across Render services.

Expected effect:

- Cleaner production topology.
- No hidden coupling between Web lifecycle and background jobs.

#### Move C4: Render deployment completion

Target files:

- `Dockerfile`
- `render.yaml` (new, when deployment wiring is implemented)
- environment/config documentation

Concrete changes:

- Split Web and Worker start commands.
- Keep browser/runtime dependencies only where needed.
- Configure Redis and PostgreSQL environment wiring for Render.

Expected effect:

- Final production-ready architecture:
  - Web
  - Worker
  - Redis
  - PostgreSQL

## 11. Verification Matrix

The following must be re-verified after each Arc:

### 11.1 Scrape flow

- `/scrape`
- `/scrape/run`
- `/scrape/status/<job_id>`
- `/scrape/result/<job_id>`
- `/api/scrape/status/<job_id>`
- `/api/scrape/jobs`
- preview registration flow

### 11.2 Data integrity

- `save_scraped_items_to_db()`
- `Product` latest values
- `Variant` inventory and price updates
- `ProductSnapshot` append-only behavior
- retry safety and duplicate suppression

### 11.3 Admin/UI

- product detail editing
- manual add flow
- dashboard and index listing
- scrape tracker UI

### 11.4 Exports

- Shopify product CSV
- Shopify stock update CSV
- Shopify price update CSV
- eBay export CSV

### 11.5 Observability

- `job_id`-correlated logs
- queue wait time
- site-level scrape duration
- browser restart count
- selector heal success / failure metrics

## 12. Risks and Controls

### Risk 1: Export breakage due to persistence drift

Control:

- Keep `Product`, `Variant`, and `ProductSnapshot` contracts stable.
- Treat `services/product_service.py` and `routes/export.py` as regression anchors.

### Risk 2: Queue cutover breaks polling UX

Control:

- Keep response payloads stable.
- Use compatibility adapters and feature flags.

### Risk 3: Scheduler duplication after process separation

Control:

- Remove scheduler ownership from the Web process before final Render split.

### Risk 4: Browser pooling introduces state leakage

Control:

- Reuse browser instance only.
- Always create fresh contexts/pages per job.
- Clear cookies/storage per context lifecycle.

### Risk 5: Alert storm from scraper instability

Control:

- Add dedupe key, cooldown window, severity level, and rate limiting to alert dispatch.
- Ensure notifier failure never fails the main scrape flow.

## 13. First Implementation Step After Approval

If this plan is approved, implementation starts at:

- `Arc 1`
- `Move A1`

No `Arc 2` or `Arc 3` work begins before `Arc 1` is completed and verified.
