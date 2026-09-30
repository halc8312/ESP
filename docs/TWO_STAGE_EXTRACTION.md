# Two-stage extraction and shared access controls

## Release status

`RECORDCITY_LISTING_ENABLED` defaults to false. Existing extraction stays on
the detail-first path and its 100-item cap. The opt-in prototype supports
standard JSON-LD `ItemList` product cards, 500 items, 10 pages and partial
checkpoints. It does **not** implement unverified RecordCity-specific listing
markup. Current RecordCity listing HTML could not be obtained: a normal GET
returned HTTP 403. Synthetic fixtures prove the contract, not live-site support.

Do not enable the flag until real keyword/category/filtered listing fixtures,
pagination, representative images and missing-field counts are verified.
Initial card saves perform zero image downloads. A separate durable thumbnail
ledger fetches one image per card in bounded, owner-balanced batches and keeps
the placeholder until a managed image is available. Source CDN URLs are never
made public as a workaround. All other sites retain their current extraction
path and 100-item cap. Their list-first adapters are future work.

The split topology stores media on the web service's disk. Staged detail and
thumbnail jobs deliver validated bytes through the existing private web channel
using `WEB_INTERNAL_HOST`/`WEB_INTERNAL_PORT` and a purpose-specific HMAC with
the shared `SECRET_KEY`. No known development signing key is accepted. Slots
are immutable and bound to the product, job token, image index and payload;
the web route rechecks the active database claim before accepting an upload.
Local integration tests exercise worker download, signed ingress, snapshot
update and public media GET. Actual Render channel/disk behavior still needs
verification before enabling listing extraction; a worker-local `/media/` path
alone is not evidence that the public image is accessible.

The legacy non-preview RQ full-save path still has a worker-local image cache.
The standard preview-then-register UI caches on the web process. This release
does not claim to repair historical images or every legacy full-save API path.

SNKRDUNK apparel IDs 300058 and 721913 still require actual successful page
HTML/structured data before extending the target-matched parser. No guessed
price or stock fix is included.

## Active shared controls

Production requires the existing shared `REDIS_URL`/`VALKEY_URL`. Atomic
site-scoped leases cover all supported marketplace HTTP attempts and browser
operations, across users and workers, with these initial bounds:

| Control | Default / bound |
| --- | --- |
| Per-site start interval | 2 seconds; production minimum 1 second |
| Per-site concurrency | 1; configurable maximum 2 |
| Owned lease | 180 seconds, renewed every 30 seconds |
| Interval / capacity wait | At most 120 seconds |
| Access refusal / CAPTCHA pause | 600 seconds |
| HTTP 429 pause | Retry-After, bounded to 60–3600 seconds |
| Target server 5xx pause | 30 seconds |
| Tracked extraction/detail budget | 120 admitted requests, 900 seconds |
| Production extraction admission | 3 queued/running jobs per owner, 50 globally |
| Detail-job admission | 20 per owner, 100 globally |

`<SITE>_ACCESS_INTERVAL_SECONDS` and `<SITE>_ACCESS_CONCURRENCY` customize the
first two values within their bounds. `SCRAPE_MAX_ACTIVE_JOBS_PER_USER` allows
1–20 jobs. Cooldowns stop a new operation immediately, preserve available
partial results and expose a safe retry delay. They do not switch to another
IP/provider. The existing configured transport can still be used initially.
Scrapling hidden retries are disabled; each application retry/redirect must
acquire admission. Browser scope extends through cleanup. If cancellation
cleanup cannot be confirmed, renewal stops and the lease remains until expiry.
Refused admission does not consume the job's request count. Provider API
authentication/quota errors stop that operation, while only confirmed target
response evidence can pause the shared marketplace site.

These controls count top-level documents and explicit HTTP fetch attempts.
Staged detail/thumbnail image downloads admit every HTTP hop, including
redirects, and observe refusal/cooldown responses before closing their lease.
Browser image/XHR subresources and legacy image-download callers are not
individually paced by this governor. Images for a selected detail job are
bounded to eight and use existing host, size and timeout validation.
Queue admission and owner-balanced detail refill reduce monopolization; the
existing RQ queue is retained and is not a strict per-owner round-robin queue.

Deadline checks occur at admission, finalization and immediately before the
single persistence commit. Completed durable writes are not retroactively
marked failed merely because the deadline passes after commit. Wait heartbeats
do not change product progress timestamps or overwrite partial products.
RecordCity's existing detail-first path stops its internal overfetch once the
requested number of validated products survives the user's filters.

## Selected detail lifecycle

Nullable columns introduced by Alembic `20260930_0024` make NULL mean legacy
complete. New shallow cards preserve unknown stock; manual selection does not
promote them to inventory one. Re-importing a card never erases verified details
or edits. New staged products do not publish source-price fallbacks.

Only selected registration/pricelist additions or authenticated public-token
availability checks request details. Owner, shop, URL, request token and lease
are rechecked before saving. Image I/O occurs outside the final DB transaction,
with a request-specific cache namespace. Old jobs cannot overwrite product
state, translations, or the new request's image artifacts. Stale-request cache
orphans may remain and should be included in future image retention cleanup.

Thumbnail batches contain at most ten images, with one active batch per owner
and five globally. Each batch is bounded to 30 HTTP attempts and 300 seconds.
Only admitted first image requests count toward the five image-attempt limit;
cooldown or capacity denial retains the claim for later recovery without
exhausting image attempts. Queue dispatch has its own five-attempt bound.
Physical batch reservations retain their original owner and job ID through
product, shop, source, snapshot or image changes, independently of demand
state. Recovery probes them without requiring the old image to remain valid.
Unknown or uninspected jobs are not reissued, and an expired running
image-upload token is never renewed. Only confirmed queue termination or the
owning worker's fully unwound synchronous I/O releases a reservation; normal
completion refills the next fair batch immediately rather than waiting for
the periodic recovery job.
The thumbnail ledger checks owner, owned shop, product URL and exact current
snapshot/image identity before queueing, fetching or publishing. It updates
images only, leaving price, stock and detail state untouched.

Selected demand beyond queue capacity remains durable. Worker startup and
the existing scheduled recovery refill it by owner; unselected cards are never
fetched. Existing queued RQ tasks are checked before replacement, and Redis
uncertainty does not permit duplicate enqueue. Failures retain a bounded
backoff and require an explicit retry; no infinite automatic retry loop is added.
Recovery rechecks the selected source and owner/shop scope under the claim
lock and final update; editing a URL alone does not select its replacement.
Translation follows detail completion with source-hash deduplication and the
existing protection for manually edited English fields.

Public POST requests validate token, owner, shop, visibility, deletion, CSRF
and rate limits. GET polling is read-only and browser polling stops after two
minutes. Unknown stock is labelled as unverified; a confirmed result refreshes
price before the customer clicks Add again. Confirmed sold/deleted products
hide Add even if old variant quantities remain. No source URL, site, cost or
internal failure text is exposed.
Public requests cannot clear a future retry deadline when request metadata
changes. Explicit owner corrections can re-arm the corrected product.

Thumbnail refresh uses a separate read-only token-scoped batch GET. It accepts
at most 50 visible, owner-matched product IDs and returns managed image URLs
only. The browser checks currently visible missing-image cards in one batch
every five seconds, stops after five minutes or any failed response, and never
starts image/detail work from the GET. Applying a thumbnail does not change
the current price, availability or Add state.
Delivered images also refresh the matching open Quick View. A later detail
response may fill an empty gallery from independently cached managed images,
without replacing its price, stock, description or existing gallery selection.

## Patrol and operations

RecordCity patrol updates only matched, explicit JPY price/availability from
one fetched page. Ambiguous stock/price retains last verified values and backs
off. Eligible products include visible, active, unexpired owner-matched
pricelists, including list-only products; unselected/deferred cards are excluded.
Existing cross-shop catalogs remain eligible when both shops belong to that
same owner; foreign-owned shops cannot make a catalog-only product eligible.

Run the full CI suite, isolated Redis admission tests, Docker validation and
split-worker startup checks before rollout. Redis tests only use a loopback
test endpoint and unique test keys, never production Redis.
The migrations are additive; do not downgrade a populated schema. Application
rollback must retain every applied revision file, including `20260930_0024`
and `20260930_0025`, so old code can recognize the already-applied head.
Verify legacy operation against that schema before
publishing any rollback ref. No new Render resources or environment changes
are required for the gated release.
