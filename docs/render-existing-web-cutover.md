# Render Existing-Web Cutover

This runbook is for the path where the current Render Web Service stays in place and only these resources are added:

- `esp-worker`
- `esp-keyvalue`
- `esp-postgres`

It does **not** create a new Render Web Service. The existing Web Service keeps its current disk and keeps serving `/media` from the same `IMAGE_STORAGE_PATH`.
The existing Web Service also stays outside Blueprint management for this cutover path; any instance type change on that service is done manually in the Render Dashboard `Scaling` tab.
All add-ons for this path must be created in the **same Render region as the existing Web Service** so private-network connection strings work. In the current production layout, that region is `singapore`.

## Goal

1. Keep the current Render Web Service alive
2. Provision Postgres / Key Value / Worker
3. Move data from SQLite at `sqlite:////var/data/mercari.db` into Postgres
4. Switch the existing Web Service from `inmemory` to `rq`
5. Keep rollback to the old single-web shape simple

## Repo Files Used for This Path

- Add-ons Blueprint: `render.existing-web-addons.yaml`
- DB migration command: `flask existing-web-db-migrate`
- Existing web scrape contract: `routes/scrape.py`
- Queue backend selection: `services/queue_backend.py`
- Worker runtime contract: `services/worker_runtime.py`
- Existing web media path: `services/image_service.py`

## Why This Path Works in This Repo

- The web app can run with `SCRAPE_QUEUE_BACKEND=rq` and `WEB_SCHEDULER_MODE=disabled`; web scheduler auto-mode only stays enabled for `inmemory`. See `app.py`.
- The worker runtime requires `SCRAPE_QUEUE_BACKEND=rq` and `REDIS_URL`. See `services/worker_runtime.py`.
- Durable job state lives in `scrape_jobs` and `scrape_job_events`, so web and worker must share the same database. See `models.py`, `services/scrape_job_store.py`, and `routes/api.py`.
- Images are still served from `IMAGE_STORAGE_PATH`, so the current web disk can stay in use. See `services/image_service.py` and `app.py`.

## Step 1: Provision Only the Add-on Resources

Do **not** import `render.yaml` for this path.

Import `render.existing-web-addons.yaml` as a **custom Blueprint path** instead and create:

- `esp-worker`
- `esp-keyvalue`
- `esp-postgres`

This Blueprint must show only those three resources in the preview. It must **not** show the existing Web Service as a create or update target.
This Blueprint must also target the same region as the existing Web Service. For the current production environment, `esp-worker`, `esp-keyvalue`, and `esp-postgres` must all be created in `singapore`.

Do not change the current Web Service env vars yet.

## Region Guardrail

Render private networking is region-scoped. Internal Postgres / Key Value URLs work only when the connecting service and datastore are in the same workspace and the same region.

If `esp-worker`, `esp-keyvalue`, or `esp-postgres` were created in `oregon` by mistake:

1. Leave the existing production Web Service in place.
2. Delete the mistaken Oregon add-ons.
3. Re-import `render.existing-web-addons.yaml` and recreate the add-ons in `singapore`.
4. Re-copy the Singapore connection strings from the recreated resources.
5. Only then continue with schema preparation and migration from the existing Web shell in Singapore.

Render does not support moving an existing service or datastore to a different region. The recovery path is delete-and-recreate, not move-in-place.

## Existing Web Scaling Note

If you want to reduce the existing Web Service from `Standard` to `Starter`, do that in the Render Dashboard on the existing service's `Scaling` screen.

- You can downgrade the existing Web Service before creating the add-ons if cost control is the top priority.
- The safer sequence is to keep the existing Web Service on `Standard` until the worker, Postgres, and Key Value resources are created and the database cutover is complete, then downgrade the existing Web Service to `Starter` last.

This repo does not attempt to manage the existing Web Service plan through Blueprint YAML for this path.

## Step 2: Prepare the Destination Postgres Schema

Run schema preparation and migration from the **existing Web Service shell in Singapore** so the command can read `sqlite:////var/data/mercari.db` from the current disk and connect to the new Singapore Postgres instance over the private network.

Either prepare the destination schema with the migration command itself:

```powershell
flask existing-web-db-migrate --source-url sqlite:////var/data/mercari.db --destination-url <POSTGRES_DATABASE_URL> --prepare-destination-schema --dry-run
```

Or do it explicitly first:

```powershell
flask db-smoke --require-backend postgresql --apply-migrations
```

Use the Postgres `DATABASE_URL` while running the explicit schema preparation path.

## Step 3: Dry-Run the SQLite -> Postgres Migration

Run the migration plan first without writing rows:

```powershell
flask existing-web-db-migrate --source-url sqlite:////var/data/mercari.db --destination-url <POSTGRES_DATABASE_URL> --dry-run
```

This prints:

- selected tables
- source and destination row counts
- missing source columns from older SQLite schemas
- blockers such as non-empty destination tables

## Step 4: Run the Actual Migration

When the dry-run is clean and the destination is empty, run:

```powershell
flask existing-web-db-migrate --source-url sqlite:////var/data/mercari.db --destination-url <POSTGRES_DATABASE_URL>
```

The command intentionally refuses to write into non-empty destination tables. This is the safe re-run guard.

## Step 5: Verify Row Counts

After the write finishes, run:

```powershell
flask existing-web-db-migrate --source-url sqlite:////var/data/mercari.db --destination-url <POSTGRES_DATABASE_URL> --verify-only
```

The verify-only pass compares row counts table-by-table and reports mismatches as blockers.

## Step 6: Configure the Worker

Set these env vars on `esp-worker`:

- `SECRET_KEY`
  Use the same value as the existing Web Service.
- `DATABASE_URL`
  Managed from `esp-postgres`
- `REDIS_URL`
  Managed from `esp-keyvalue`
- `SCRAPE_QUEUE_BACKEND=rq`
- `WORKER_ENABLE_SCHEDULER=1`
- `WARM_BROWSER_POOL=1`
- `ENABLE_SHARED_BROWSER_RUNTIME=1`
- `BROWSER_POOL_WARM_SITES=mercari`
- `MERCARI_USE_BROWSER_POOL_DETAIL=1`
- `MERCARI_PATROL_USE_BROWSER_POOL=1`
- `SNKRDUNK_USE_BROWSER_POOL_DYNAMIC=1`
- `WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP=1`
- `WORKER_BACKLOG_WARN_COUNT=25`
- `WORKER_BACKLOG_WARN_AGE_SECONDS=900`
- `OPERATIONAL_ALERT_WEBHOOK_URL`
  Optional manual secret

Deploy the worker and confirm it starts with `python worker.py`.
Also confirm in the Render Dashboard that `esp-worker` was created with **2 instances**.
Also confirm the worker service is in `singapore`.

## Step 7: Switch the Existing Web Service

After Postgres data is migrated and the worker is healthy, update the **existing** Web Service env vars.

Required env vars for the existing Web Service:

- `SECRET_KEY`
  Keep the current value and match the worker
- `DATABASE_URL=<POSTGRES_DATABASE_URL>`
- `REDIS_URL=<KEY_VALUE_CONNECTION_STRING>`
- `SCRAPE_QUEUE_BACKEND=rq`
- `WEB_SCHEDULER_MODE=disabled`
- `SCHEMA_BOOTSTRAP_MODE=auto`
- `IMAGE_STORAGE_PATH=/var/data/images`

Keep the current disk attached to the existing Web Service. Do not change the web service into a worker, and do not add a second web service for this path.
Do not switch these env vars before the add-ons exist and the SQLite -> Postgres migration has completed.
Use the internal connection strings from the Singapore `esp-postgres` and Singapore `esp-keyvalue` resources. These internal URLs are valid only for same-workspace, same-region private networking. Do not use internal URLs from a different region.

## Step 8: Redeploy the Existing Web Service

Once the existing Web Service env vars are updated, redeploy it.

Expected post-cutover health contract:

- `runtime_role=web`
- `queue_backend=rq`
- `scheduler_enabled=false`

## Step 9: Smoke Checks

Run a web smoke check against the **existing** Web Service URL:

```powershell
flask render-postdeploy-smoke --base-url https://<current-web-url> --expect-queue-backend rq --expect-runtime-role web --expect-scheduler-mode disabled --retries 4 --retry-delay-seconds 2
```

For authenticated checks:

```powershell
flask render-postdeploy-smoke --base-url https://<current-web-url> --expect-queue-backend rq --expect-runtime-role web --expect-scheduler-mode disabled --retries 4 --retry-delay-seconds 2 --username <smoke-user> --password <smoke-password> --ensure-user
```

Also verify manually:

- `/healthz` returns `200`
- `/login` returns `200`
- `/scrape` returns `200` after login
- `/api/scrape/jobs` returns `200` after login
- one preview scrape job moves from `queued` to `completed`
- one persist scrape job reaches `/scrape/result/<job_id>`

## Human Checks in Render

Before cutover:

1. Open the existing Web Service and confirm whether Auto-Deploy is enabled.
2. Decide whether to leave the existing Web Service on `Standard` until the end or downgrade it first.
3. If you downgrade it, do so from the existing Web Service `Scaling` screen, not from Blueprint.
4. Confirm the existing Web Service region is `singapore`.
5. If Oregon add-ons already exist by mistake, delete those add-ons first. Do not delete the existing Web Service.
6. Create add-ons by importing `render.existing-web-addons.yaml` as a custom Blueprint path.
7. In the Blueprint preview, confirm only `esp-worker`, `esp-keyvalue`, and `esp-postgres` appear.
8. In the Blueprint preview or created resource pages, confirm all three add-ons target `singapore`.
9. After creation, open `esp-worker` and confirm it has 2 instances.
10. Open `esp-postgres` and copy its Singapore internal connection string from the Render Dashboard.
11. Open `esp-keyvalue` and copy its Singapore internal connection string from the Render Dashboard.
12. Run the SQLite -> Postgres migration from the existing Web shell in Singapore.
13. Only after the migration is verified, update env vars on the existing Web Service.

Render Dashboard reference points:

- Existing Web instance type: existing Web Service -> `Scaling`
- Existing Web Auto-Deploy: existing Web Service -> `Settings` / deploy settings
- Postgres connection string: `esp-postgres` -> connection info / internal connection string
- Key Value connection string: `esp-keyvalue` -> connection info / internal connection string

## Rollback

If anything fails after the existing Web Service is switched:

1. Put the existing Web Service back on:
   - `DATABASE_URL=sqlite:////var/data/mercari.db`
   - `SCRAPE_QUEUE_BACKEND=inmemory`
   - `WEB_SCHEDULER_MODE=auto` or `enabled`
2. Redeploy the existing Web Service
3. Stop using the worker-backed path
4. Keep `esp-worker`, `esp-keyvalue`, and `esp-postgres` isolated until the issue is fixed
5. Fix the issue locally first
6. Re-run:

```powershell
flask existing-web-db-migrate --source-url sqlite:////var/data/mercari.db --destination-url <POSTGRES_DATABASE_URL> --verify-only
```

and then repeat the cutover

## Re-run Policy

- The migration command blocks if destination tables already contain rows
- The safe re-run path is:
  - discard and recreate the destination Postgres database, or
  - manually empty the migrated tables, then re-run from dry-run
- Do not attempt to merge partially migrated rows by hand in the application database
