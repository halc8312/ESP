# Worker deadline, memory and ownership safeguards

Baseline investigated: `add57104e770ee964e0306b44e376229efa6fe8c`.
The last confirmed OOM was 2026-10-10 02:29:31.817 UTC on
`srv-d77fj5p5pdvs7399cmh0` (2 GiB). Memory growth preceded the final patrol
hang. The evidence does not identify Python, Playwright Node or Chromium as
the principal allocator, nor prove that the DB connection failures were
caused by memory pressure. These changes address verified unbounded work
and unsafe recovery paths; production root-cause resolution still requires
observation after an approved rollout.

## Behavior

- RQ worker patrols run in an owned child, reusing browsers within a batch and
  stopping them immediately when the batch ends. Selection, attempt
  checkpoint, fetch, DB application and commit all live inside that boundary.
  The parent admits no new work after cancellation/deadline until the old
  process tree is verified stopped. An unverifiable stop quarantines it.
- Whole-item and whole-batch deadlines include a reserved cleanup budget.
  Atomic progress/result files avoid a blocking partial Pipe frame.
  Attempt checkpoints/backoff prevent a repeatedly hung first item from
  indefinitely starving later items. Product locks and snapshot checks
  reject results from changed/deleted products.
- PostgreSQL session advisory ownership serializes patrol generations even
  when a Redis lease expires. Redis owner checks and the parent's conservative
  lease expiry watchdog stop expired work. The guarantee is no stale Product
  writes after a successor acquires the PostgreSQL patrol lock. A Redis check
  and PostgreSQL commit are not a cross-store atomic operation.
- Scheduler heartbeat writes compare the Redis owner token atomically.
  An expired owner cannot overwrite a successor's completion/health record.
  Lock-renewal failure stops admission and cancels owned work.
  Production worker heartbeat I/O also runs in an owned helper with a
  five-second budget, so native DNS cannot indefinitely occupy the scheduler
  thread. Patrol completion reserves that budget inside its batch deadline.
- Browser permits are released when the actual asyncio Task finishes.
  Repeated cancellation cannot bypass bounded cleanup. Uncertain cleanup
  quarantines the runtime and site beyond the normal lease TTL.
- Mercari capture reads only the matching item API response in bounded CDP
  chunks: at most 1 MiB per body, 2 MiB total including discarded bytes, and
  eight response attempts. Oversized responses are aborted. Unsupported CDP
  uses DOM fallback. Patrol skips image carousel/photo-only reload work.
- Browser recycling defaults to 50 tasks or 900 seconds. It is checked
  between tasks; the owned deadline boundary stops an in-progress hang.
  RecordCity profiles are isolated by owned process/generation.
- Recovery notification/thumbnail/detail/translation phases have separate
  started/finished/status/count/error evidence. A later successful phase
  cannot hide an earlier failure. Production RQ recovery uses owned children.
  `no_pending` means an empty recovery scan, not proof of completed delivery.
- DB defaults bound driver connection, SQL execution and lock waits.
  Native limits complement owned process deadlines; they do not make every
  DNS call or Python function independently interruptible.

## Default configuration

No live Render settings or `render.yaml` are changed by this patch.

| Setting | Default | Meaning |
| --- | --- | --- |
| `PATROL_SUPERVISED_FETCH_ENABLED` | true for worker + RQ | Whole patrol owned process boundary; false retains the legacy path, without a hard whole-item bound. |
| `PATROL_ITEM_TIMEOUT_SECONDS` | 120 | Item wall-clock budget including persistence and reserved cleanup. |
| `PATROL_BATCH_TIMEOUT_SECONDS` | 780 | Entire patrol wall-clock budget. |
| `RECOVERY_PHASE_TIMEOUT_SECONDS` | 60 | Each recovery phase including reserved cleanup. |
| `RECOVERY_BATCH_TIMEOUT_SECONDS` | 240 | All recovery phases. |
| `RECOVERY_HEARTBEAT_FRESHNESS_SECONDS` | 900 | Feature evidence freshness. |
| `DB_CONNECT_TIMEOUT_SECONDS` | 5 | Native driver connection timeout. Explicit URL/libpq configuration is respected. |
| `DB_STATEMENT_TIMEOUT_MS` | 15000 | PostgreSQL statement timeout unless already explicitly configured. |
| `DB_LOCK_TIMEOUT_MS` | 5000 | PostgreSQL lock timeout unless already explicitly configured. |
| `BROWSER_POOL_MAX_TASKS_BEFORE_RESTART` | 50 | Recycle before the next task; a site-specific prefix can override it. |
| `BROWSER_POOL_MAX_RUNTIME_SECONDS` | 900 | Recycle before the next task once this age is reached. |
| `PATROL_CLEANUP_TIMEOUT_SECONDS` / `PATROL_KILL_TIMEOUT_SECONDS` | 10 / 5 | Validated hard-stop budget. Lease cutoff reserves their sum plus 5 seconds; an insufficient scheduler TTL refuses admission. |

Browser recycling environment variable names and quarantine recovery
procedures are documented in the browser runtime and marketplace access
runbook. Explicit recycling zero values still disable that respective limit.

## Required regression gates

CI uses Python 3.11, disposable local Redis/PostgreSQL and the pinned
Playwright Chromium. No marketplace request or production DB mutation is
needed. The PostgreSQL tests reject external hosts and non-test databases.

```sh
python -m pytest -q
```

| Fault / workload | Required result |
| --- | --- |
| API body read or JSON/RPC never returns; context/browser close never returns; SIGTERM ignored | Deadline plus fixed cleanup budget returns; owned tree is reaped using verified identities, or the generation remains quarantined and the next request is refused. No detached timeout thread continues the operation. |
| Two cancellations during settlement of an uncooperative RPC | Result remains cleanup-unverified; permit and site ownership are not declared clean while the real Task is still running. |
| Partial IPC output or malformed/oversized result | Parent deadline still expires; no blocking `recv_bytes` after `poll`. |
| Lease A expires and B is admitted | A cannot change B's shared heartbeat. B cannot acquire patrol DB ownership while A's advisory connection remains. Once B is admitted, no stale A Product write may appear. Site quarantine remains until verified cleanup. Already-sent remote requests do not have an exactly-once guarantee. |
| Item DB checkout/statement/commit hangs | Outer item deadline kills its DB-owning generation; the parent does not enter DB persistence. A bounded attempt checkpoint means later eligible products can progress in a later batch. |
| PID reused, child detached/reparented, leader already exited | An unrelated reused PID is never signalled. Observed owned descendants remain tracked. Missing ownership visibility is a failure to verify, not a successful stop. |
| Notification fails and translation succeeds; enqueue fails; phase times out | Aggregate degraded and the specific feature failed/degraded/stale/quarantined; process liveness can remain healthy. Empty scans remain `no_pending`. |
| Purge races with insert/apply/late translation result | No orphan/cross-owner deletion/deadlock/resurrection; request snapshots survive and rollback restores every deleted child. See the FK runbook. |

Real Linux `/proc`, Chromium and PostgreSQL tests must execute, not merely
skip, before rollout. Mocked process snapshots and SQLite FK tests do not
replace those gates. The restricted local execution environment has no
`/proc` or PostgreSQL server; Chromium installation failed because the CDN
returned a truncated archive. The CI configuration supplies these resources;
an unobserved CI run must not be reported as passed.

## Approved rollout observation

After approval, verify the old unsupervised worker generation and its browsers
are stopped before activating the first new supervised generation. The
baseline code does not participate in the new PostgreSQL ownership lock.
Deploy the reviewed commit once and confirm its exact SHA.
Record UTC start/completion, item IDs, timeout/cleanup outcomes, quarantines,
feature health, and memory for at least 24 hours, preferably 48 hours of mixed
workload (the prior recurrence interval reached about 9.8 hours), including
the original `m93745541162` item.

Use authorized read-only process inspection to record PID/start time,
parent/session, process category, RSS, and PSS from `smaps_rollup` where
available. Sum PSS for a tree; RSS can double count shared pages. Record
Python, Node and Chromium separately and include cgroup memory and child
counts. Avoid exporting command lines, environment or credentials. Where
process inspection is unavailable, request that diagnostic capability
separately rather than inferring an allocator from the aggregate chart.

For a repeatable local/staging workload, run at least 200 representative
sequential items across four 50-item cycles with the same browser-recycle
policy. After warmup, the final-cycle median tree PSS should be within the
greater of 64 MiB or 10% of the first post-warmup cycle; all cycle peaks
should remain below 90% of the 2 GiB limit. These are rollout acceptance
targets, not measurements already achieved. Sustained positive growth,
unreaped children, repeated same-item hangs, or any OOM fails the gate.
Inspect a lightweight patrol separately from full detail/image work.

## Approval boundary

Merging/deploying, changing environment values or DB/network permissions,
restarting/killing a production worker, clearing a production quarantine,
and purging/repairing live rows require approval. This patch performs none
of those operations. Do not clear a lease/quarantine merely because its TTL
expired or memory fell; require proof the owning resources stopped.
