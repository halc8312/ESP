from datetime import datetime, timedelta, timezone

import pytest

from services.worker_runtime import inspect_patrol_heartbeat


class Redis:
    def __init__(self, payload):
        self.payload = payload

    def hgetall(self, _key):
        return self.payload


def healthy_payload(now):
    return {
        "runtime_role": "worker",
        "last_patrol_completed_at": (now - timedelta(minutes=16)).isoformat(),
        "last_patrol_status": "completed",
        "last_patrol_selected_count": "50",
        "last_patrol_successful_count": "50",
        "last_patrol_error_count": "0",
        "last_patrol_unprocessed_count": "0",
    }


@pytest.mark.parametrize(("started_age", "expected"), [(779, "ok"), (781, "stale")])
def test_started_batch_deadline_overrides_previous_fresh_success(started_age, expected):
    now = datetime.now(timezone.utc)
    payload = healthy_payload(now)
    payload.update(last_patrol_started_at=(now - timedelta(seconds=started_age)).isoformat(),
        last_patrol_batch_timeout_seconds="780")
    assert inspect_patrol_heartbeat(Redis(payload), key="key", freshness_seconds=1200, now=now) == expected


def test_first_batch_hang_is_stale_after_its_deadline():
    now = datetime.now(timezone.utc)
    payload = {"runtime_role": "worker", "last_patrol_started_at": (now - timedelta(seconds=781)).isoformat(),
        "last_patrol_batch_timeout_seconds": "780"}
    assert inspect_patrol_heartbeat(Redis(payload), key="key", freshness_seconds=1200, now=now) == "stale"


@pytest.mark.parametrize("status", ["deadline_exceeded", "cancelled", "quarantined"])
def test_unsuccessful_batch_end_never_counts_as_success(status):
    now = datetime.now(timezone.utc)
    payload = healthy_payload(now)
    payload.update(last_patrol_completed_at=now.isoformat(), last_patrol_status=status)
    assert inspect_patrol_heartbeat(Redis(payload), key="key", freshness_seconds=1200, now=now) == "failed"


def test_partial_batch_counts_include_unprocessed_without_claiming_success():
    now = datetime.now(timezone.utc)
    payload = healthy_payload(now)
    payload.update(last_patrol_successful_count="40", last_patrol_unprocessed_count="10")
    assert inspect_patrol_heartbeat(Redis(payload), key="key", freshness_seconds=1200, now=now) == "degraded"
    payload["last_patrol_unprocessed_count"] = "11"
    assert inspect_patrol_heartbeat(Redis(payload), key="key", freshness_seconds=1200, now=now) == "failed"


@pytest.mark.parametrize(("successful", "errors", "unprocessed", "discarded", "inconclusive", "expected"), [
    (40, 1, 4, 2, 3, "degraded"),
    (0, 0, 0, 25, 25, "no_observations"),
    (0, 1, 0, 24, 25, "failed"),
    (40, 1, 4, 2, 4, "failed"),
])
def test_distinct_result_buckets_conserve_selected_count(successful, errors, unprocessed, discarded, inconclusive, expected):
    now = datetime.now(timezone.utc)
    payload = healthy_payload(now)
    payload.update({
        "last_patrol_successful_count": str(successful),
        "last_patrol_error_count": str(errors),
        "last_patrol_unprocessed_count": str(unprocessed),
        "last_patrol_discarded_count": str(discarded),
        "last_patrol_inconclusive_count": str(inconclusive),
    })
    assert inspect_patrol_heartbeat(Redis(payload), key="key", freshness_seconds=1200, now=now) == expected
