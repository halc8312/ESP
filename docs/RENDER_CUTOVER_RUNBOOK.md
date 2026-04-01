# Render Cutover Runbook

This runbook is for `Arc 3 / Move C4`, when the first paid Render split is explicitly approved.

It does not replace the current single-web production path. Until cutover is approved, keep the existing Render Web Service on `SCRAPE_QUEUE_BACKEND=inmemory`.

## Goal

Activate the dormant `render.yaml` safely, verify the first paid split locally first, and keep rollback to the current single-web production path simple.

Expected first paid shape:

- `esp-web`
- `esp-worker`
- `esp-keyvalue`
- `esp-postgres`

The intended initial recurring cost stays aligned with the budget guardrail in `docs/BACKEND_RENEWAL_EXECUTION_PLAN.md`. Re-check Render pricing on the actual activation date before provisioning.

## Local Gate

Do not provision paid Render services until all of the following pass locally.

1. `flask predeploy-check --target single-web`
2. `flask schema-drift-check`
3. `flask render-blueprint-audit`
4. `flask render-budget-guardrail-audit`
5. `flask render-dashboard-inputs`
6. `flask render-local-split-checklist --blueprint-path render.yaml`
7. `flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict`
8. `py -3 -m pytest tests -q`

If you want the exact operator sequence in one payload, generate it with:

```powershell
flask render-cutover-checklist --base-url https://<esp-web-url> --username <smoke-user> --password <smoke-password>
```

If `search-mercari-fixture` is advisory-only and reports `search_skeleton`, that is not a paid-cutover blocker by itself. It means the local search dump is not a rendered result page.

## Required Local Services

Bring up the local stand-ins before the cutover gate.

```powershell
docker compose -f docker-compose.local.yml up -d
```

If you want the exact local split rehearsal contract in one payload, generate it with:

```powershell
flask render-local-split-checklist --blueprint-path render.yaml
```

This checklist also reports whether the recommended local PostgreSQL and Redis endpoints are actually reachable before you run the stricter cutover gates.

If you want to run the same rehearsal with the repo-pinned local PostgreSQL/Redis env defaults in one command, use:

```powershell
flask render-local-split-readiness
```

If you want the full operator bundle before the first paid activation, use:

```powershell
flask render-cutover-brief --base-url https://<esp-web-url> --username <smoke-user> --password <smoke-password>
```

Expected local equivalents:

- PostgreSQL
- Redis
- Flask web app
- `worker.py`

## Render Service Mapping

Use the dormant Blueprint in `render.yaml`.

### Web

- Service name: `esp-web`
- Health check: `/healthz`
- Queue backend: `rq`
- Scheduler: disabled on web
- Image storage path: `/var/data/images`

### Worker

- Service name: `esp-worker`
- Start command: `python worker.py`
- Scheduler owner: enabled on exactly one worker
- Shared browser runtime: enabled

### Data Stores

- Key Value: `esp-keyvalue`
- Postgres: `esp-postgres`

## Secret Env Vars

These stay manual and must be filled in when the Blueprint is applied.

- `SECRET_KEY`
- `SELECTOR_ALERT_WEBHOOK_URL`
- `OPERATIONAL_ALERT_WEBHOOK_URL`

## Managed Env Wiring

These should stay managed by the Blueprint and should not be copied by hand from a local shell.

- `DATABASE_URL`
- `REDIS_URL`
- `SCRAPE_QUEUE_BACKEND`
- `WEB_SCHEDULER_MODE`
- `SCHEMA_BOOTSTRAP_MODE`
- `IMAGE_STORAGE_PATH`
- `WORKER_ENABLE_SCHEDULER`
- `WARM_BROWSER_POOL`
- `ENABLE_SHARED_BROWSER_RUNTIME`
- `BROWSER_POOL_WARM_SITES`
- `MERCARI_USE_BROWSER_POOL_DETAIL`
- `MERCARI_PATROL_USE_BROWSER_POOL`
- `SNKRDUNK_USE_BROWSER_POOL_DYNAMIC`
- `WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP`
- `WORKER_BACKLOG_WARN_COUNT`
- `WORKER_BACKLOG_WARN_AGE_SECONDS`

## Safe Activation Order

1. Leave the current single-web production service unchanged.
2. Re-run the local gate immediately before provisioning.
3. Import/sync `render.yaml` without pointing it at the current single-web service.
4. Fill secret env vars.
5. Provision `esp-postgres` and `esp-keyvalue`.
6. Deploy `esp-web` and confirm `/healthz` returns `200`.
7. Deploy `esp-worker` and compare its startup logs against `flask render-worker-postdeploy-checklist --blueprint-path render.yaml`.
8. Run `flask render-postdeploy-smoke --base-url https://<esp-web-url> --retries 4 --retry-delay-seconds 2`.
9. If a smoke user already exists, rerun with `--username <smoke-user> --password <smoke-password>`. If it does not exist yet, add `--ensure-user` so authenticated `/scrape` and `/api/scrape/jobs` are checked too.
10. Run one preview scrape smoke.
11. Run one persist scrape smoke.
12. Confirm status polling, result page rendering, and one persisted product path.
13. Only after those checks pass, plan any traffic or operator cutover.

## First Render Checks

After provisioning, verify at minimum:

- `esp-web` health check stays green
- `esp-worker` starts and remains healthy
- `SCRAPE_QUEUE_BACKEND=rq`
- `WEB_SCHEDULER_MODE=disabled` on web
- `WORKER_ENABLE_SCHEDULER=1` only on the intended worker
- `/login`, `/scrape`, `/api/scrape/jobs` do not return `500`
- authenticated `/api/scrape/jobs` does not return `500` or redirect back to `/login`
- Redis-backed queue jobs move from `queued` to `completed`
- One real scrape job reaches `/api/scrape/status/<job_id>` and `/scrape/result/<job_id>`

## Rollback

If any first-cutover check fails:

1. Stop using the new Render services for operator workflows.
2. Keep the existing single-web production service as the live fallback.
3. Do not mutate the legacy single-web service into `rq` mode.
4. Fix the issue locally first.
5. Re-run `render-cutover-readiness --strict` before trying the paid split again.
