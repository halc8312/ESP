# Single-Web Redeploy Runbook

This runbook is for the current Render production shape: one Web Service running on `SCRAPE_QUEUE_BACKEND=inmemory`.

Use it for routine UI / DOM / parser fixes while the paid split (`web + worker + postgres + key value`) is still dormant.

## Goal

Redeploy the current single-web production service without accidentally mixing in worker/RQ assumptions or persistent DB schema drift.

## Local Gate

Before redeploying the current single-web service, pass these locally:

1. `flask single-web-redeploy-readiness`

If you want the exact operator sequence in one payload, generate it with:

```powershell
flask single-web-redeploy-checklist --base-url https://<current-web-url> --username <smoke-user> --password <smoke-password>
```

## Required Current-Service Constraints

- Keep `SCRAPE_QUEUE_BACKEND=inmemory`
- Do not add worker-only Redis/RQ env vars to the current single-web service
- Keep `WEB_SCHEDULER_MODE` on `auto` or `enabled`
- Do not repurpose the current single-web service into the split worker

## Safe Redeploy Order

1. Re-run the local gate.
2. Confirm the current service is still configured for `SCRAPE_QUEUE_BACKEND=inmemory`.
3. Deploy the single-web service.
4. Confirm `/healthz` returns `200`.
5. Run `flask single-web-postdeploy-smoke --base-url https://<current-web-url> --retries 4 --retry-delay-seconds 2`.
6. If a smoke user exists, rerun with `--username <smoke-user> --password <smoke-password> --ensure-user`.
7. Confirm `/login`, `/scrape`, and `/api/scrape/jobs` do not return `500`.
8. Confirm authenticated `/scrape` and `/api/scrape/jobs` also do not return `500`.

## Rollback

If the redeploy fails:

1. Keep `SCRAPE_QUEUE_BACKEND=inmemory`.
2. Restore the last known-good single-web deployment or release.
3. Fix the issue locally first.
4. Re-run `flask predeploy-check --target single-web` before retrying.
