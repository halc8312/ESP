# Product purge and translation suggestion referential integrity

## Finding

The observed PostgreSQL error for product 1331 on 2026-10-09 and
2026-10-10 at approximately 03:00 UTC is a valid foreign key preventing a
parent deletion while `translation_suggestions` still references it. It
does **not** establish an orphaned suggestion or connect this separate purge
failure to the worker OOM incident.

Both `models.TranslationSuggestion.product_id` and migration
`20260420_0006` intentionally declare `ForeignKey("products.id")` without an
`ON DELETE` action. The old trash purge removed only `variants` and
`product_snapshots`, then deleted Product. Missing dependent cleanup caused
the error; model and migration are consistent with each other.

Production row contents, owner consistency, deployed constraint validation
state, and running translation state for product 1331 have **not** been
queried during this code change. The previous read-only external database
reader was blocked by an empty allowlist. No allowlist, production data,
schema, or deployment was changed.

## Policy and implementation

Soft deletion and restore preserve translation history. Permanent deletion
removes product-specific ephemeral data only after locking and revalidating
an eligible trash Product. Manual deletion additionally validates the
logged-in owner; scheduled deletion validates the 30-day cutoff.

`services/product_purge.py` handles every current Product foreign key in one
transaction:

| Child table | Permanent deletion behavior |
| --- | --- |
| `translation_suggestions` | Delete all owned job states, including a running translation. Late worker CAS updates affect zero rows. |
| `image_processing_jobs` | Preserve Product while queued/running physical image work remains; delete terminal job history after drain. |
| `price_list_items` | Delete the Product's entries, preserving the PriceList itself. |
| `product_thumbnail_jobs` | Preserve any non-null physical `job_id` reservation; delete only after drain. |
| `variants`, `product_snapshots` | Delete product-owned detail rows. |
| `catalog_request_items` | Retain customer title/price/quantity snapshots; set only `product_id` to NULL. |

Owner mismatches in translation, image, thumbnail, or pricelist references
block that Product before any child deletion. Another eligible Product can
still be purged. Unknown future references continue to fail closed through
the restrictive FK; the coverage test makes addition of a new Product FK
visible. Failure after child cleanup rolls back the complete transaction.

No `ON DELETE CASCADE` migration was added. Explicit cleanup fixes existing
deployments without dropping/recreating a constraint or broadly deleting
data outside the owner-validated trash path. The FK stays as the final
concurrency guard. Product deletion while an incompatible child is inserted
remains rejected; PostgreSQL's parent row lock also serializes FK checks.
The lock-order rationale follows PostgreSQL 18's
[row-lock conflict rules](https://www.postgresql.org/docs/18/explicit-locking.html#LOCKING-ROWS).

## Translation concurrency

Purge locks Product before deleting suggestions. Translation creation,
manual application, and automatic result persistence now lock the live
owned Product **before** a suggestion write. This removes the inverse
child-write → parent-write order that could deadlock with purge on
PostgreSQL. Translation computation does not hold this transaction open.

Creation and worker claims reject missing, soft-deleted, or foreign-owned
parents. Manual/automatic application does not update trashed products.
If purge commits while a translator is computing, `mark_succeeded`,
`mark_failed`, and `mark_running` use UPDATE-only predicates and cannot
recreate the deleted row. Automatic persistence returns false for a missing
row or parent. The existing random worker token continues to fence replaced
worker claims.

## Other deletion paths reviewed

The normal application permanent-delete paths are manual `/trash/purge`
and scheduled `purge_old_trash`; both use the same helper. Other direct
`session.delete(Product)` calls occur in CLI single-web/stack smoke fixture
cleanup and the legacy `cleanup_db.py` script. They retain restrictive FK
behavior and may fail if unexpected translation/list/image references
exist. They were not changed to bypass owner/trash policy and were not run
against production. `cleanup_db.py` in particular selects by sourcing URL
without owner scope; do not use it as a production repair tool.

## Validation and remaining production gate

`tests/test_trash_referential_integrity.py` enables `PRAGMA foreign_keys=ON`
on every checked-out test connection and asserts enforcement is active.
Tests cover valid-FK rejection of raw parent deletion, every translation
state, all Product FK children, preservation of request snapshots,
soft-delete/restore history, owner mismatch, active image reservations,
transaction rollback, and a late worker result after deletion.

The focused FK/trash/translation/deferred-detail/catalog-request suite passed
213 tests locally on Python 3.12 with the repository's pinned SQLAlchemy and
psycopg dependencies. The production image uses Python 3.11; the disposable
PostgreSQL and production-version gates below are still required.

The SQLite automatic result test observes the actual emitted SQL order and
compiles the parent query using the PostgreSQL dialect, asserting
`FOR UPDATE` precedes the child UPDATE. This validates the generated lock
intent; it is **not** a PostgreSQL concurrency execution test.

`tests/test_trash_postgresql_concurrency.py` adds five opt-in real PostgreSQL
tests with independent SQLAlchemy sessions, bounded threading Events, and
server-side `pg_blocking_pids` observations. Each test creates and drops only
its own UUID schema; the test engine's search path excludes `public`. The
fixture rejects nonlocal hosts and database names without `test` before
connecting. This is a disposable local/CI database gate, never a production
database repair command.

To run against a local/CI PostgreSQL service with an `esp_test` database:

```bash
ESP_TEST_POSTGRES_URL=postgresql+psycopg://esp_test:esp_test@127.0.0.1:5432/esp_test \
  python -m pytest tests/test_trash_postgresql_concurrency.py -q
```

Without `ESP_TEST_POSTGRES_URL`, these five tests explicitly skip. No local
PostgreSQL server was available during the initial change, so skips do not
count as evidence that PostgreSQL races passed. The production rollout gate
requires an executed passing result for:

1. Purge holds the parent lock; a new suggestion INSERT must wait and then
   fail FK validation after purge commits, leaving no orphan row.
2. Automatic result persistence holds the parent lock; purge waits, then
   either sees the completed live row or removes its trashed children after
   the application transaction commits. There must be no lock-order cycle.
3. Translation computes outside a transaction; purge commits first; a late
   result must update zero rows and leave Product/Suggestion absent.
4. Inject an exception after deleting suggestions; rollback must restore
   parent and every dependent row unchanged.
5. A foreign owner cannot lock an eligible deletion target; a mismatched
   child owner blocks deletion before any rows are removed.

## Read-only production checks still needed

These checks can be run through an already authorized internal read-only
connection. Do not open an external allowlist or paste credentials to run
them. They deliberately avoid translation text, passwords, and tokens.

```sql
BEGIN READ ONLY;
SELECT conname, convalidated, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'translation_suggestions'::regclass
  AND contype = 'f';

SELECT p.id, p.deleted_at, ts.status, count(*) AS suggestions,
       min(ts.created_at) AS oldest, max(ts.updated_at) AS newest,
       bool_or(ts.user_id <> p.user_id) AS owner_mismatch
FROM products p
LEFT JOIN translation_suggestions ts ON ts.product_id = p.id
WHERE p.id = 1331
GROUP BY p.id, p.deleted_at, ts.status;

SELECT count(*) AS orphan_count
FROM translation_suggestions ts
LEFT JOIN products p ON p.id = ts.product_id
WHERE p.id IS NULL;

SELECT count(*) AS mismatched_owner_count
FROM translation_suggestions ts
JOIN products p ON p.id = ts.product_id
WHERE ts.user_id <> p.user_id;
ROLLBACK;
```

Inspect running image/thumbnail reservations before approving deletion. A
queued/running background-removal row is deliberately not swept by age or
status guessing; resolving an actually abandoned physical job requires a
separate confirmed drain/cancel operation.
