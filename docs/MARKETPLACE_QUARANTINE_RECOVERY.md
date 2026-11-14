# Marketplace quarantine recovery

An expired admission lease does not prove that its browser stopped. Quarantine
markers have no TTL and block all new site admissions until termination is
verified. Never clear every site, use FLUSHDB, or remove a successor's token.
This runbook does not authorize a production restart or quarantine write.

## Read-only inspection

Use an already-authorized private Python/Flask shell in the affected worker's
environment. Do not print environment variables, full Redis/database URLs,
browser command lines, or credentials.

```python
from services.marketplace_access import inspect_marketplace_quarantines
inspect_marketplace_quarantines(sites=["mercari", "recordcity"])
```

The result contains only site, exact owner token, generation and, for a Linux
unsupervised owner, its PID/start ticks. It is private operator evidence and
must not be returned in public catalog or health responses. Inspection does
not acquire/release a lease or clear a marker.

## Supervised patrol

The supervisor clears only its own touched sites and generation after it has
terminated and reaped the recorded process tree. Any termination, visibility,
Redis-clear or deadline failure leaves the supervisor fail-closed. Do not bypass
that failure by removing a marker. Restore diagnostic visibility/Redis access,
then verify that exact old process generation and all recorded descendants are
gone before using its generation-scoped recovery API.

`clear_marketplace_generation_quarantines(generation, sites=..., deadline=...)`
is an internal supervisor API whose caller must already hold the verified-tree
termination evidence. Its per-call dedicated Redis connection uses remaining
socket/connect budgets without changing the shared client's settings. OS DNS
resolution is not bounded by a socket timeout; if DNS stalls, the caller must
remain fail-closed rather than treating cleanup as successful.

## Unsupervised RQ/detail browser owners

1. Obtain approval for any required restart/process termination. Quarantine
   inspection alone needs no production mutation.
2. Correlate the exact marker with its owning process, Render instance, browser
   runtime and logs. Record the owner's PID/start ticks and the complete owned
   child/resource identities before stopping it. PID alone is insufficient.
3. Verify every recorded identity is reaped/gone. Also verify the complete old
   Render instance/cgroup has no residual resources; missing a newly spawned or
   reparented Chromium invalidates the proof. A successful close call, lower RSS,
   idle worker or 180-second lease expiry is insufficient.
4. After approval for the explicit token-scoped write, construct the evidence
   and clear only that owner using the private operator API:

```python
from services.marketplace_access import (
    QuarantineStopProof, clear_marketplace_owner_quarantine,
)

proof = QuarantineStopProof(
    generation=observed_generation,
    owner_identity=observed_owner_pid_and_start_ticks,
    resource_identities=complete_recorded_process_identities,
    evidence_reference=approved_incident_or_restart_event_reference,
)
clear_marketplace_owner_quarantine(observed_site, observed_exact_owner, proof=proof)
```

The API validates generation/owner identity, requires a nonempty evidence
reference and a tree containing its owner, and independently refuses any PID
that still has the recorded start ticks. Missing process visibility fails
closed. It cannot reconstruct omitted descendants, so complete tree/cgroup
proof remains an operator responsibility. It uses exact SREM/ZREM operations;
it cannot remove a different owner's lease/quarantine.

Anonymous legacy UUIDs and non-Linux portable identities cannot be verified by
this API. Keep them blocked until their entire old service-instance ownership
can be established and a separate reviewed recovery procedure is approved.
Never infer ownership from timing alone.

## Verification after an authorized clear

Read the quarantine state again, verify unrelated/successor tokens remain, then
observe a bounded healthy fetch and its actual cleanup. Confirm no old resource
generation, late DB write, duplicate site operation or leaked runtime permit
remains. Production real-Redis and repeated-browser gates are still required;
fakeredis/Lua unit tests establish script semantics only.
