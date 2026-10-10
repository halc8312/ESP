"""Exercise heartbeat fencing with real Lua, without changing live Redis."""
import os
import threading
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask

import app as app_module


@pytest.fixture
def ownership_redis():
    url = os.environ.get("MARKETPLACE_ACCESS_TEST_REDIS_URL")
    if url:
        from redis import Redis
        connection = Redis.from_url(url, decode_responses=True)
    else:
        fakeredis = pytest.importorskip("fakeredis")
        connection = fakeredis.FakeRedis(decode_responses=True)
    prefix = "esp:test:ownership:" + uuid.uuid4().hex
    yield connection, prefix
    keys = list(connection.scan_iter(match=prefix + "*"))
    if keys:
        connection.delete(*keys)
    connection.close()


def test_expired_scheduler_cannot_overwrite_successors_health(monkeypatch, ownership_redis):
    connection, prefix = ownership_redis
    application = Flask(__name__)
    application.config.update(
        ESP_RUNTIME_ROLE="worker", SCHEDULER_HEARTBEAT_KEY=prefix + ":health",
    )
    lock = SimpleNamespace(name=prefix + ":lock", local=SimpleNamespace(token="old-owner"))
    application.extensions["esp_scheduler_lock_handle"] = app_module._RedisSchedulerLockHandle(
        lock, threading.Event(), SimpleNamespace(join=lambda **kwargs: None),
    )
    monkeypatch.setattr(app_module, "_get_scheduler_heartbeat_connection", lambda _app: connection)
    connection.set(lock.name, "old-owner", ex=120)
    app_module._write_scheduler_heartbeat(application, event="patrol_started", last_patrol_run_id="old")
    assert connection.hget(prefix + ":health", "last_patrol_run_id") == "old"
    connection.set(lock.name, "new-owner", ex=120)
    connection.hset(prefix + ":health", mapping={"event": "patrol_started", "last_patrol_run_id": "new"})

    app_module._write_scheduler_heartbeat(application, event="patrol_completed", last_patrol_run_id="old")

    assert connection.hget(prefix + ":health", "event") == "patrol_started"
    assert connection.hget(prefix + ":health", "last_patrol_run_id") == "new"
    assert application.extensions["esp_scheduler_heartbeat_error"] == "ownership_lost"


def test_candidate_scheduler_writes_own_diagnostics(monkeypatch, ownership_redis):
    connection, prefix = ownership_redis
    application = Flask(__name__)
    application.config.update(
        ESP_RUNTIME_ROLE="worker", SCHEDULER_LOCK_BACKEND="redis",
        SCHEDULER_HEARTBEAT_KEY=prefix + ":health",
    )
    application.extensions["esp_scheduler_start_attempted"] = True
    connection.hset(prefix + ":health", mapping={"event": "patrol_completed"})
    monkeypatch.setattr(app_module, "_get_scheduler_heartbeat_connection", lambda _app: connection)

    app_module._write_scheduler_heartbeat(application, event="scheduler_lock_not_acquired")

    assert connection.hget(prefix + ":health", "event") == "patrol_completed"
    key = application.extensions["esp_scheduler_heartbeat_key"]
    assert key.startswith(prefix + ":health:candidate:")
    assert connection.hget(key, "event") == "scheduler_lock_not_acquired"


def test_failed_lease_renewal_stops_admission_and_cancels_owned_work(monkeypatch):
    import redis

    class ImmediateEvent:
        def __init__(self):
            self.set_flag = False

        def wait(self, _seconds):
            return self.set_flag

        def set(self):
            self.set_flag = True

        def is_set(self):
            return self.set_flag

    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def join(self, **kwargs):
            pass

    class LostLock:
        name = "esp:test:scheduler:lock"
        local = SimpleNamespace(token="owner")

        def acquire(self, **kwargs):
            return True

        def extend(self, *args, **kwargs):
            raise redis.exceptions.LockNotOwnedError("expired")

    application = Flask(__name__)
    application.config.update(SCHEDULER_LOCK_BACKEND="redis", REDIS_URL="redis://unused", ESP_RUNTIME_ROLE="worker")
    shutdown_calls = []
    application.extensions.update(
        esp_scheduler=SimpleNamespace(shutdown=lambda **kwargs: shutdown_calls.append(kwargs)),
        esp_scheduler_started=True,
    )
    monkeypatch.setattr(redis.Redis, "from_url", lambda _url, **kwargs: SimpleNamespace(lock=lambda *a, **k: LostLock()))
    monkeypatch.setattr(app_module, "threading", SimpleNamespace(Event=ImmediateEvent, Thread=ImmediateThread))

    handle = app_module._try_acquire_redis_scheduler_lock(application)

    assert isinstance(handle, app_module._RedisSchedulerLockHandle)
    assert application.extensions["esp_scheduler_ownership_lost"].is_set()
    assert application.extensions["esp_scheduler_started"] is False
    assert application.extensions["esp_scheduler_lock_acquired"] is False
    assert shutdown_calls == [{"wait": False}]


def test_local_lease_expiry_stops_work_before_redis_ttl(monkeypatch):
    application = Flask(__name__)
    application.extensions["esp_scheduler_lease_expires_monotonic"] = 120
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 99)
    assert app_module._scheduler_operation_cancelled(application) is False
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 100)
    assert app_module._scheduler_operation_cancelled(application) is True
    application.extensions["esp_scheduler_lease_expires_monotonic"] = 150
    assert app_module._scheduler_operation_cancelled(application) is False


def test_scheduler_delegates_entire_patrol_without_parent_db_work(monkeypatch):
    class Scheduler:
        def __init__(self):
            self.jobs = {}

        def add_job(self, **kwargs):
            self.jobs[kwargs["id"]] = kwargs

        def get_jobs(self):
            return [SimpleNamespace(id=name) for name in self.jobs]

    application = Flask(__name__)
    application.config.update(
        ESP_RUNTIME_ROLE="worker", PATROL_SUPERVISED_FETCH_ENABLED=True,
        PATROL_BATCH_SIZE=50, PATROL_ITEM_TIMEOUT_SECONDS=120,
        PATROL_BATCH_TIMEOUT_SECONDS=780, REDIS_URL="redis://test.invalid",
    )
    scheduler = Scheduler()
    application.extensions["esp_scheduler"] = scheduler
    application.extensions["esp_scheduler_lock_handle"] = app_module._RedisSchedulerLockHandle(
        SimpleNamespace(name="test:lock", local=SimpleNamespace(token=b"owner")),
        threading.Event(), SimpleNamespace(join=lambda **kwargs: None),
    )
    calls, heartbeat = [], []
    monkeypatch.setattr(app_module, "_write_scheduler_heartbeat", lambda _app, **fields: heartbeat.append(fields))
    from services.monitor_service import MonitorService
    monkeypatch.setattr(MonitorService, "check_stale_products", lambda **kwargs: pytest.fail("parent patrol DB entrypoint called"))
    monkeypatch.setattr("services.patrol_supervisor.get_supervised_patrol_runner", lambda: SimpleNamespace(
        run_batch=lambda **kwargs: calls.append(kwargs) or {
            "status": "completed", "selected_count": 3, "successful_count": 1,
            "error_count": 0, "discarded_count": 1, "inconclusive_count": 1,
        },
    ))

    app_module._register_scheduler_jobs(application)
    scheduler.jobs["patrol_job"]["func"]()

    assert len(calls) == 1
    assert calls[0]["limit"] == 50
    assert calls[0]["item_timeout"] == 120
    assert calls[0]["scheduler_ownership"]["owner_token_hex"] == b"owner".hex()
    assert calls[0]["cancelled"]() is False
    assert heartbeat[-1]["last_patrol_discarded_count"] == 1
    assert heartbeat[-1]["last_patrol_inconclusive_count"] == 1


def test_custom_cleanup_budget_advances_lease_stop_cutoff(monkeypatch):
    monkeypatch.setenv("PATROL_CLEANUP_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("PATROL_KILL_TIMEOUT_SECONDS", "10")
    application = Flask(__name__)
    application.extensions["esp_scheduler_lease_expires_monotonic"] = 120
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 84)
    assert app_module._scheduler_operation_cancelled(application) is False
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 85)
    assert app_module._scheduler_operation_cancelled(application) is True


def test_ttl_too_short_for_cleanup_refuses_scheduler_admission():
    application = Flask(__name__)
    application.config.update(SCHEDULER_LOCK_BACKEND="redis", REDIS_URL="redis://unused", SCHEDULER_LOCK_TTL_SECONDS=30)
    assert app_module._try_acquire_redis_scheduler_lock(application) is False
    assert application.extensions["esp_scheduler_lock_reason"] == "ttl_insufficient_for_cleanup"


@pytest.mark.parametrize("status", ["written", "ownership_lost", "timeout", "quarantined"])
def test_production_heartbeat_io_is_owned_and_never_falls_back(monkeypatch, status):
    application = Flask(__name__)
    application.config.update(
        ESP_RUNTIME_ROLE="worker", SCRAPE_QUEUE_BACKEND="rq", TESTING=False,
        SCHEDULER_HEARTBEAT_ENABLED=True, REDIS_URL="redis://test.invalid",
    )
    application.extensions["esp_scheduler_lock_handle"] = app_module._RedisSchedulerLockHandle(
        SimpleNamespace(name="test:lock", local=SimpleNamespace(token=b"owner")),
        threading.Event(), SimpleNamespace(join=lambda **kwargs: None),
    )
    calls = []
    monkeypatch.setattr("services.heartbeat_transport.bounded_write", lambda request, **kwargs: (
        calls.append((request, kwargs)) or {"status": status}
    ))
    monkeypatch.setattr(app_module, "_get_scheduler_heartbeat_connection", lambda _app: pytest.fail("parent Redis I/O"))
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 100)

    app_module._write_scheduler_heartbeat(application, _deadline=102, event="patrol_completed")

    assert calls[0][1]["timeout_seconds"] == 2
    assert calls[0][0]["owner_token_hex"] == b"owner".hex()
    assert calls[0][0]["mapping"]["event"] == "patrol_completed"
    assert application.extensions["esp_scheduler_heartbeat_error"] == (None if status == "written" else status)
    lost = application.extensions.get("esp_scheduler_ownership_lost")
    assert bool(lost and lost.is_set()) == (status in {"ownership_lost", "quarantined"})


def test_startup_recovery_is_published_only_after_scheduler_ownership(monkeypatch):
    application = Flask(__name__)
    application.config.update(
        ESP_RUNTIME_ROLE="worker", SCRAPE_QUEUE_BACKEND="rq", TESTING=False,
        SCHEDULER_HEARTBEAT_ENABLED=True, REDIS_URL="redis://test.invalid",
    )
    calls = []
    monkeypatch.setattr("services.heartbeat_transport.bounded_write", lambda request, **kwargs: (
        calls.append(request) or {"status": "written"}
    ))
    app_module._write_scheduler_heartbeat(application, event="recovery_phase_completed", last_recovery_detail_status="failed")
    assert ":candidate:" in calls[0]["heartbeat_key"]
    application.extensions["esp_scheduler_lock_handle"] = app_module._RedisSchedulerLockHandle(
        SimpleNamespace(name="test:lock", local=SimpleNamespace(token=b"owner")),
        threading.Event(), SimpleNamespace(join=lambda **kwargs: None),
    )
    app_module._write_scheduler_heartbeat(application, event="scheduler_started")
    assert ":candidate:" not in calls[1]["heartbeat_key"]
    assert calls[1]["mapping"]["last_recovery_detail_status"] == "failed"
    assert calls[1]["mapping"]["event"] == "scheduler_started"
    assert "esp_scheduler_pending_recovery_heartbeat" not in application.extensions


def test_startup_local_health_snapshot_does_not_read_redis(monkeypatch):
    application = Flask(__name__)
    monkeypatch.setattr(app_module, "_read_scheduler_heartbeat", lambda _app: pytest.fail("startup Redis read"))
    snapshot = app_module.get_scheduler_health_snapshot(application, include_heartbeat=False)
    assert snapshot["heartbeat"] is None
    assert snapshot["started"] is False
