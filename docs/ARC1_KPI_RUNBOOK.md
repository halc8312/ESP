# Arc 1 KPI Runbook

- Updated: 2026-03-19
- Scope: low-cost closeout checks for `Arc 1`

## Goal

Close `Arc 1` with one small, explicit measurement pass instead of further feature work.

This runbook is intentionally designed to stay low-cost:

- use only 1-2 fixed queries per site
- keep `max_items` small
- compare a few controlled scenarios rather than broad crawling

## Prerequisites

- Do not run this until external scrape traffic is explicitly approved.
- Keep probes off-peak where possible.
- Save each run as JSON so the comparisons are reproducible.
- The probe is intended to be read-only:
  - it runs scrape/search code paths only
  - it must not call `save_scraped_items_to_db()`
  - it must not enqueue jobs, register previews, or mutate export/admin state

## Harness

Use:

- `[scripts/arc1_kpi_probe.py](/f:/ESP-main/ESP-main/scripts/arc1_kpi_probe.py)`

The probe measures:

- total scrape duration
- detail expansion duration
- items/min
- success/failure counts
- extraction strategy counts
- field source counts

Mercari-only metrics:

- payload capture enabled/success counts
- payload-used counts
- payload rescue counts
- shadow compare mismatch counts
- DOM fallback field counts during payload-first mode

Each JSON artifact also includes run metadata:

- UTC timestamp
- site
- search URL
- label / env label
- active flag set
- detail concurrency override
- Python/platform info
- current git SHA when available

## Recommended Matrix

### Rakuma

1. `concurrency=1`
2. default async concurrency

Example:

```powershell
py -3 scripts/arc1_kpi_probe.py `
  --site rakuma `
  --search-url "<fixed-rakuma-search-url>" `
  --max-items 5 `
  --detail-concurrency 1 `
  --env-label "local-offpeak" `
  --output artifacts/arc1_kpi/rakuma-c1.json
```

```powershell
py -3 scripts/arc1_kpi_probe.py `
  --site rakuma `
  --search-url "<fixed-rakuma-search-url>" `
  --max-items 5 `
  --detail-concurrency 4 `
  --env-label "local-offpeak" `
  --output artifacts/arc1_kpi/rakuma-c4.json
```

### SNKRDUNK

1. `concurrency=1`
2. default async concurrency

Example:

```powershell
py -3 scripts/arc1_kpi_probe.py `
  --site snkrdunk `
  --search-url "<fixed-snkrdunk-search-url>" `
  --max-items 5 `
  --detail-concurrency 1 `
  --env-label "local-offpeak" `
  --output artifacts/arc1_kpi/snkrdunk-c1.json
```

```powershell
py -3 scripts/arc1_kpi_probe.py `
  --site snkrdunk `
  --search-url "<fixed-snkrdunk-search-url>" `
  --max-items 5 `
  --detail-concurrency 4 `
  --env-label "local-offpeak" `
  --output artifacts/arc1_kpi/snkrdunk-c4.json
```

### Mercari

Run three scenarios against the same fixed search URL:

1. DOM only
2. capture-only
3. payload-first

Example:

```powershell
py -3 scripts/arc1_kpi_probe.py `
  --site mercari `
  --search-url "<fixed-mercari-search-url>" `
  --max-items 5 `
  --detail-concurrency 1 `
  --env-label "local-offpeak" `
  --output artifacts/arc1_kpi/mercari-dom.json
```

```powershell
py -3 scripts/arc1_kpi_probe.py `
  --site mercari `
  --search-url "<fixed-mercari-search-url>" `
  --max-items 5 `
  --detail-concurrency 1 `
  --mercari-capture `
  --env-label "local-offpeak" `
  --output artifacts/arc1_kpi/mercari-capture.json
```

```powershell
py -3 scripts/arc1_kpi_probe.py `
  --site mercari `
  --search-url "<fixed-mercari-search-url>" `
  --max-items 5 `
  --detail-concurrency 1 `
  --mercari-use-payload `
  --env-label "local-offpeak" `
  --output artifacts/arc1_kpi/mercari-payload.json
```

## Closeout Criteria

`Arc 1` can be closed when the recorded probe results show:

- no regression in core scrape flows
- `A1`: detail expansion is improved or at least not materially worse than `concurrency=1`
- `A2`: structured extraction is visible in `field_source_counts` and CSS fallback is reduced
- `Mercari`: capture-only and payload-first both operate safely without breaking the result shape
- `A3`: forced selector failure paths alert correctly and do not break scrape completion

## Known Non-Blocking Warnings

Current local warning debt:

- `[services/product_service.py](/f:/ESP-main/ESP-main/services/product_service.py)` still uses `datetime.utcnow()`

This is not an `Arc 1` blocker, but it should be cleaned up before or during `Arc 2`.
