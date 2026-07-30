import json
from datetime import datetime, timedelta, timezone

import app as app_module


def test_healthz_returns_minimal_liveness_payload(client):
    response = client.get("/healthz")

    assert response.status_code == 200

    payload = response.get_json()
    assert payload == {
        "status": "ok",
        "runtime_role": "test",
    }


def test_readyz_returns_ready_when_database_is_available(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ready",
        "checks": {"database": "ok"},
        "runtime_role": "test",
        "queue_backend": "inmemory",
        "scheduler_enabled": False,
    }


def test_readyz_returns_503_when_database_is_unavailable(client, monkeypatch):
    def _raise_connection_error():
        raise RuntimeError("database is unavailable")

    with monkeypatch.context() as local_patch:
        local_patch.setattr(app_module, "SessionLocal", _raise_connection_error)
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable"},
        "runtime_role": "test",
        "queue_backend": "inmemory",
        "scheduler_enabled": False,
    }


def test_readyz_requires_redis_for_rq_backend(client):
    client.application.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "",
        }
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {
            "database": "ok",
            "redis": "unavailable",
        },
        "runtime_role": "test",
        "queue_backend": "rq",
        "scheduler_enabled": False,
    }


def test_stack_readyz_reports_only_non_secret_dependency_states(client):
    client.application.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "",
        }
    )

    response = client.get("/stack-readyz")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {
            "database": "ok",
            "redis": "unavailable",
            "worker": "unavailable",
            "scheduler": "unavailable",
            "patrol": "unavailable",
        },
        "runtime_role": "test",
        "queue_backend": "rq",
        "scheduler_enabled": False,
    }


class _FakeReadinessRedis:
    def __init__(self, *, worker_payload=None, worker_ttl=90, scheduler_payload=None):
        self.worker_key = "esp:worker:heartbeat:test-host:123:abc"
        self.worker_payload = worker_payload
        self.worker_ttl = worker_ttl
        self.scheduler_payload = scheduler_payload or {}
        self.closed = False

    def ping(self):
        return True

    def scan_iter(self, match):
        if self.worker_payload is not None:
            yield self.worker_key

    def get(self, key):
        if key == self.worker_key:
            return self.worker_payload
        return None

    def ttl(self, key):
        return self.worker_ttl

    def hgetall(self, key):
        return dict(self.scheduler_payload)

    def close(self):
        self.closed = True


def _heartbeat_payload(
    *,
    recorded_at,
    runtime_role="worker",
    event="scheduler_started",
):
    return {
        "runtime_role": runtime_role,
        "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
        "event": event,
        "last_patrol_completed_at": recorded_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "last_patrol_status": "completed",
        "last_patrol_selected_count": "1",
        "last_patrol_error_count": "0",
    }


def test_readyz_requires_only_redis_for_rq_backend(client, monkeypatch):
    redis_client = _FakeReadinessRedis()
    client.application.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "redis://example.test:6379/0",
        }
    )
    monkeypatch.setattr(
        app_module,
        "_create_readiness_redis_client",
        lambda _redis_url: redis_client,
    )

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json()["checks"] == {
        "database": "ok",
        "redis": "ok",
    }
    assert redis_client.closed is True


def test_stack_readyz_requires_live_worker_and_scheduler_for_rq_backend(client, monkeypatch):
    now = datetime.now(timezone.utc)
    redis_client = _FakeReadinessRedis(
        worker_payload=json.dumps(_heartbeat_payload(recorded_at=now)),
        scheduler_payload=_heartbeat_payload(recorded_at=now),
    )
    client.application.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "redis://example.test:6379/0",
        }
    )
    monkeypatch.setattr(
        app_module,
        "_create_readiness_redis_client",
        lambda _redis_url: redis_client,
    )

    response = client.get("/stack-readyz")

    assert response.status_code == 200
    assert response.get_json()["checks"] == {
        "database": "ok",
        "redis": "ok",
        "worker": "ok",
        "scheduler": "ok",
        "patrol": "ok",
    }
    assert redis_client.closed is True


def test_stack_readyz_reports_only_stale_state_for_expired_worker_heartbeat(client, monkeypatch):
    now = datetime.now(timezone.utc)
    redis_client = _FakeReadinessRedis(
        worker_payload=json.dumps(
            _heartbeat_payload(recorded_at=now - timedelta(minutes=5))
        ),
        scheduler_payload=_heartbeat_payload(recorded_at=now),
    )
    client.application.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "redis://example.test:6379/0",
            "WORKER_HEARTBEAT_FRESHNESS_SECONDS": 60,
        }
    )
    monkeypatch.setattr(
        app_module,
        "_create_readiness_redis_client",
        lambda _redis_url: redis_client,
    )

    response = client.get("/stack-readyz")

    assert response.status_code == 503
    assert response.get_json()["checks"] == {
        "database": "ok",
        "redis": "ok",
        "worker": "stale",
        "scheduler": "ok",
        "patrol": "ok",
    }


def test_stack_readyz_fails_when_latest_patrol_processed_no_items_successfully(
    client,
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    scheduler_payload = _heartbeat_payload(recorded_at=now)
    scheduler_payload["last_patrol_selected_count"] = "5"
    scheduler_payload["last_patrol_error_count"] = "5"
    redis_client = _FakeReadinessRedis(
        worker_payload=json.dumps(_heartbeat_payload(recorded_at=now)),
        scheduler_payload=scheduler_payload,
    )
    client.application.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "redis://example.test:6379/0",
        }
    )
    monkeypatch.setattr(
        app_module,
        "_create_readiness_redis_client",
        lambda _redis_url: redis_client,
    )

    response = client.get("/stack-readyz")

    assert response.status_code == 503
    assert response.get_json()["checks"]["scheduler"] == "ok"
    assert response.get_json()["checks"]["patrol"] == "failed"


def test_stack_readyz_rejects_scheduler_heartbeat_from_web_role(client, monkeypatch):
    now = datetime.now(timezone.utc)
    redis_client = _FakeReadinessRedis(
        worker_payload=json.dumps(_heartbeat_payload(recorded_at=now)),
        scheduler_payload=_heartbeat_payload(
            recorded_at=now,
            runtime_role="web",
        ),
    )
    client.application.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "redis://example.test:6379/0",
        }
    )
    monkeypatch.setattr(
        app_module,
        "_create_readiness_redis_client",
        lambda _redis_url: redis_client,
    )

    response = client.get("/stack-readyz")

    assert response.status_code == 503
    assert response.get_json()["checks"]["scheduler"] == "stale"


def test_readyz_parses_string_zero_scheduler_flag_as_disabled(client):
    client.application.config["ENABLE_SCHEDULER"] = "0"

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json()["scheduler_enabled"] is False
