from types import SimpleNamespace

import pytest

from app import _should_try_redis_scheduler_lock, create_web_app, create_worker_app
from services.worker_runtime import (
    build_worker_runtime,
    emit_backlog_operational_alert,
    evaluate_backlog_issues,
    get_worker_health_snapshot,
    load_worker_runtime_settings,
    run_worker,
)


def test_create_worker_app_skips_web_routes():
    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
        }
    )

    assert app.config["ESP_RUNTIME_ROLE"] == "worker"
    assert app.config["REGISTER_BLUEPRINTS"] is False
    assert app.config["REGISTER_CLI_COMMANDS"] is False
    assert "scrape.scrape_run" not in app.view_functions


def test_create_web_app_single_service_mode_starts_scheduler_for_inmemory_queue(monkeypatch):
    captured = {}

    def fake_start_scheduler(app):
        captured["app"] = app
        app.extensions["esp_scheduler_started"] = True
        return True

    monkeypatch.setattr("app.start_scheduler", fake_start_scheduler)

    app = create_web_app(
        config_overrides={
            "TESTING": True,
            "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        }
    )

    assert app.config["ENABLE_SCHEDULER"] is True
    assert captured["app"] is app
    assert app.extensions["esp_scheduler_started"] is True


def test_create_web_app_rq_mode_keeps_scheduler_disabled():
    app = create_web_app(
        config_overrides={
            "TESTING": True,
            "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
            "SCRAPE_QUEUE_BACKEND": "rq",
        }
    )

    assert app.config["ENABLE_SCHEDULER"] is False
    assert app.extensions.get("esp_scheduler_started") is None


def test_create_web_app_honors_explicit_disabled_scheduler_mode():
    app = create_web_app(
        config_overrides={
            "TESTING": True,
            "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
            "WEB_SCHEDULER_MODE": "disabled",
        }
    )

    assert app.config["ENABLE_SCHEDULER"] is False
    assert app.extensions.get("esp_scheduler_started") is None


def test_create_web_app_runs_additive_patchset_after_alembic(monkeypatch):
    captured = []

    monkeypatch.setattr("app.bootstrap_schema", lambda mode: "alembic")
    monkeypatch.setattr("app.run_legacy_startup_migrations", lambda: captured.append("patched"))
    monkeypatch.setattr("app.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})

    app = create_web_app(
        config_overrides={
            "TESTING": True,
            "SCRAPE_QUEUE_BACKEND": "rq",
        }
    )

    assert app.extensions["esp_schema_bootstrap_mode"] == "alembic"
    assert captured == ["patched"]


def test_create_web_app_fails_fast_when_schema_drift_remains(monkeypatch):
    monkeypatch.setattr("app.bootstrap_schema", lambda mode: "alembic")
    monkeypatch.setattr("app.run_legacy_startup_migrations", lambda: {"applied": [], "errors": []})
    monkeypatch.setattr(
        "app.ensure_additive_schema_ready",
        lambda: (_ for _ in ()).throw(RuntimeError("Database schema drift remains after bootstrap: scrape_jobs.tracker_dismissed_at")),
    )

    with pytest.raises(RuntimeError, match="tracker_dismissed_at"):
        create_web_app(
            config_overrides={
                "TESTING": True,
                "SCRAPE_QUEUE_BACKEND": "rq",
            }
        )


def test_single_service_web_scheduler_does_not_require_redis_lock():
    app = create_web_app(
        config_overrides={
            "TESTING": True,
            "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
            "SCRAPE_QUEUE_BACKEND": "inmemory",
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )

    assert _should_try_redis_scheduler_lock(app) is False


def test_rq_worker_scheduler_still_prefers_redis_lock():
    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "redis://localhost:6379/0",
            "ENABLE_SCHEDULER": True,
        }
    )

    assert _should_try_redis_scheduler_lock(app) is True


def test_create_worker_app_can_start_scheduler_when_enabled(monkeypatch):
    captured = {}

    def fake_start_scheduler(app):
        captured["app"] = app
        app.extensions["esp_scheduler_started"] = True
        return True

    monkeypatch.setattr("app.start_scheduler", fake_start_scheduler)

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "ENABLE_SCHEDULER": True,
        }
    )

    assert captured["app"] is app
    assert app.extensions["esp_scheduler_started"] is True


def test_load_worker_runtime_settings_requires_rq_backend():
    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "inmemory",
        }
    )

    with pytest.raises(RuntimeError, match="requires SCRAPE_QUEUE_BACKEND=rq"):
        load_worker_runtime_settings(app)


def test_load_worker_runtime_settings_reads_worker_flags():
    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "SCRAPE_QUEUE_NAME": "worker-q",
            "REDIS_URL": "redis://example.test:6379/9",
            "RQ_BURST": "true",
            "RQ_WITH_SCHEDULER": "1",
            "WARM_BROWSER_POOL": "1",
            "WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP": "0",
            "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": "0",
            "WORKER_SELECTOR_REPAIR_LIMIT": "7",
            "WORKER_BACKLOG_WARN_COUNT": "12",
            "WORKER_BACKLOG_WARN_AGE_SECONDS": "345",
        }
    )

    settings = load_worker_runtime_settings(app)

    assert settings.queue_backend == "rq"
    assert settings.queue_name == "worker-q"
    assert settings.redis_url == "redis://example.test:6379/9"
    assert settings.burst is True
    assert settings.with_scheduler is True
    assert settings.warm_browser_pool is True
    assert settings.reconcile_stalled_jobs_on_startup is False
    assert settings.process_selector_repairs_on_startup is False
    assert settings.selector_repair_limit == 7
    assert settings.backlog_warn_count == 12
    assert settings.backlog_warn_age_seconds == 345


def test_evaluate_backlog_issues_detects_count_and_age_thresholds():
    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WORKER_BACKLOG_WARN_COUNT": 2,
            "WORKER_BACKLOG_WARN_AGE_SECONDS": 300,
        }
    )

    settings = load_worker_runtime_settings(app)
    issues = evaluate_backlog_issues(
        {
            "queued_count": 3,
            "oldest_queued_age_seconds": 400,
            "oldest_running_age_seconds": 301,
        },
        settings,
    )

    assert issues == [
        "queued_count>=2",
        "oldest_queued_age_seconds>=300",
        "oldest_running_age_seconds>=300",
    ]


def test_build_worker_runtime_uses_queue_and_worker_imports(monkeypatch):
    captured = {}

    class FakeRedis:
        def ping(self):
            captured["pinged"] = True

        @staticmethod
        def from_url(redis_url):
            captured["redis_url"] = redis_url
            return FakeRedis()

    class FakeQueue:
        def __init__(self, name, connection):
            captured["queue_name"] = name
            captured["queue_connection"] = connection

    class FakeWorker:
        def __init__(self, queues, connection):
            captured["worker_queues"] = queues
            captured["worker_connection"] = connection

        def work(self, burst, with_scheduler):
            captured["burst"] = burst
            captured["with_scheduler"] = with_scheduler

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "SCRAPE_QUEUE_NAME": "worker-q",
            "REDIS_URL": "redis://localhost:6379/2",
            "RQ_BURST": True,
            "WARM_BROWSER_POOL": True,
        }
    )

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr("services.worker_runtime.import_rq_queue", lambda: FakeQueue)
    monkeypatch.setattr("services.worker_runtime.import_rq_simple_worker", lambda: FakeWorker)
    monkeypatch.setattr("services.worker_runtime.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})
    monkeypatch.setattr("services.worker_runtime.get_job_backlog_snapshot", lambda: {"queued_count": 0, "running_count": 0})
    monkeypatch.setattr("services.worker_runtime.reconcile_stalled_jobs", lambda: ["stalled-1"])
    monkeypatch.setattr("services.worker_runtime.warm_browser_pool", lambda: captured.setdefault("warmed", True))
    monkeypatch.setattr("services.worker_runtime.close_browser_pool", lambda: captured.setdefault("closed", True))

    runtime = build_worker_runtime(app)
    status = run_worker(app)

    assert runtime.settings.queue_name == "worker-q"
    assert captured["redis_url"] == "redis://localhost:6379/2"
    assert captured["pinged"] is True
    assert captured["queue_name"] == "worker-q"
    assert isinstance(captured["queue_connection"], FakeRedis)
    assert isinstance(captured["worker_connection"], FakeRedis)
    assert captured["burst"] is True
    assert captured["with_scheduler"] is False
    assert captured["warmed"] is True
    assert captured["closed"] is True
    assert status == 0


def test_run_worker_can_skip_startup_reconcile(monkeypatch):
    captured = {}

    class FakeRedis:
        @staticmethod
        def from_url(redis_url):
            return FakeRedis()

    class FakeQueue:
        def __init__(self, name, connection):
            pass

    class FakeWorker:
        def __init__(self, queues, connection):
            pass

        def work(self, burst, with_scheduler):
            captured["worked"] = True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP": False,
        }
    )

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr("services.worker_runtime.import_rq_queue", lambda: FakeQueue)
    monkeypatch.setattr("services.worker_runtime.import_rq_simple_worker", lambda: FakeWorker)
    monkeypatch.setattr("services.worker_runtime.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})
    monkeypatch.setattr("services.worker_runtime.get_job_backlog_snapshot", lambda: {"queued_count": 0, "running_count": 0})
    monkeypatch.setattr("services.worker_runtime.reconcile_stalled_jobs", lambda: captured.setdefault("reconciled", True))
    monkeypatch.setattr("services.worker_runtime.close_browser_pool", lambda: captured.setdefault("closed", True))

    status = run_worker(app)

    assert captured.get("reconciled") is None
    assert captured["worked"] is True
    assert captured["closed"] is True
    assert status == 0


def test_run_worker_processes_selector_repairs_on_startup(monkeypatch):
    captured = {}

    class FakeRedis:
        @staticmethod
        def from_url(redis_url):
            return FakeRedis()

    class FakeQueue:
        def __init__(self, name, connection):
            pass

    class FakeWorker:
        def __init__(self, queues, connection):
            pass

        def work(self, burst, with_scheduler):
            captured["worked"] = True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": True,
            "WORKER_SELECTOR_REPAIR_LIMIT": 4,
        }
    )

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr("services.worker_runtime.import_rq_queue", lambda: FakeQueue)
    monkeypatch.setattr("services.worker_runtime.import_rq_simple_worker", lambda: FakeWorker)
    monkeypatch.setattr("services.worker_runtime.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})
    monkeypatch.setattr("services.worker_runtime.get_job_backlog_snapshot", lambda: {"queued_count": 0, "running_count": 0})
    monkeypatch.setattr("services.worker_runtime.reconcile_stalled_jobs", lambda: [])
    monkeypatch.setattr(
        "services.worker_runtime.process_pending_repair_candidates",
        lambda limit: captured.setdefault("repair_limits", []).append(limit) or {"promoted": 1},
    )
    monkeypatch.setattr("services.worker_runtime.close_browser_pool", lambda: captured.setdefault("closed", True))

    status = run_worker(app)

    assert captured["repair_limits"] == [4]
    assert captured["worked"] is True
    assert captured["closed"] is True
    assert status == 0


def test_run_worker_can_skip_selector_repairs_on_startup(monkeypatch):
    captured = {}

    class FakeRedis:
        @staticmethod
        def from_url(redis_url):
            return FakeRedis()

    class FakeQueue:
        def __init__(self, name, connection):
            pass

    class FakeWorker:
        def __init__(self, queues, connection):
            pass

        def work(self, burst, with_scheduler):
            captured["worked"] = True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": False,
        }
    )

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr("services.worker_runtime.import_rq_queue", lambda: FakeQueue)
    monkeypatch.setattr("services.worker_runtime.import_rq_simple_worker", lambda: FakeWorker)
    monkeypatch.setattr("services.worker_runtime.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})
    monkeypatch.setattr("services.worker_runtime.get_job_backlog_snapshot", lambda: {"queued_count": 0, "running_count": 0})
    monkeypatch.setattr("services.worker_runtime.reconcile_stalled_jobs", lambda: [])
    monkeypatch.setattr(
        "services.worker_runtime.process_pending_repair_candidates",
        lambda limit: captured.setdefault("repair_called", True),
    )
    monkeypatch.setattr("services.worker_runtime.close_browser_pool", lambda: captured.setdefault("closed", True))

    status = run_worker(app)

    assert captured.get("repair_called") is None
    assert captured["worked"] is True
    assert captured["closed"] is True
    assert status == 0


def test_run_worker_fails_fast_when_schema_drift_remains(monkeypatch):
    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
        }
    )

    monkeypatch.setattr(
        "services.worker_runtime.ensure_additive_schema_ready",
        lambda: (_ for _ in ()).throw(RuntimeError("Database schema drift remains after bootstrap: scrape_jobs.tracker_dismissed_at")),
    )

    with pytest.raises(RuntimeError, match="tracker_dismissed_at"):
        run_worker(app)


def test_run_worker_logs_backlog_before_and_after_reconcile(monkeypatch):
    captured = {"snapshots": []}

    class FakeRedis:
        @staticmethod
        def from_url(redis_url):
            return FakeRedis()

    class FakeQueue:
        def __init__(self, name, connection):
            pass

    class FakeWorker:
        def __init__(self, queues, connection):
            pass

        def work(self, burst, with_scheduler):
            captured["worked"] = True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WARM_BROWSER_POOL": False,
        }
    )

    def fake_backlog_snapshot():
        captured["snapshots"].append("called")
        return {"queued_count": len(captured["snapshots"]), "running_count": 0}

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr("services.worker_runtime.import_rq_queue", lambda: FakeQueue)
    monkeypatch.setattr("services.worker_runtime.import_rq_simple_worker", lambda: FakeWorker)
    monkeypatch.setattr("services.worker_runtime.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})
    monkeypatch.setattr("services.worker_runtime.get_job_backlog_snapshot", fake_backlog_snapshot)
    monkeypatch.setattr("services.worker_runtime.reconcile_stalled_jobs", lambda: [])
    monkeypatch.setattr("services.worker_runtime.close_browser_pool", lambda: captured.setdefault("closed", True))

    status = run_worker(app)

    assert captured["snapshots"] == ["called", "called"]
    assert captured["worked"] is True
    assert captured["closed"] is True
    assert status == 0


def test_run_worker_warns_when_backlog_is_unhealthy(monkeypatch):
    captured = {"warnings": []}

    class FakeRedis:
        @staticmethod
        def from_url(redis_url):
            return FakeRedis()

    class FakeQueue:
        def __init__(self, name, connection):
            pass

    class FakeWorker:
        def __init__(self, queues, connection):
            pass

        def work(self, burst, with_scheduler):
            captured["worked"] = True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WARM_BROWSER_POOL": False,
            "WORKER_BACKLOG_WARN_COUNT": 2,
            "WORKER_BACKLOG_WARN_AGE_SECONDS": 300,
        }
    )

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr("services.worker_runtime.import_rq_queue", lambda: FakeQueue)
    monkeypatch.setattr("services.worker_runtime.import_rq_simple_worker", lambda: FakeWorker)
    monkeypatch.setattr("services.worker_runtime.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})
    monkeypatch.setattr(
        "services.worker_runtime.get_job_backlog_snapshot",
        lambda: {
            "queued_count": 3,
            "running_count": 1,
            "oldest_queued_age_seconds": 400,
            "oldest_running_age_seconds": 50,
        },
    )
    monkeypatch.setattr("services.worker_runtime.reconcile_stalled_jobs", lambda: [])
    monkeypatch.setattr("services.worker_runtime.close_browser_pool", lambda: captured.setdefault("closed", True))
    monkeypatch.setattr(
        "services.worker_runtime.logger.warning",
        lambda message, *args: captured["warnings"].append(message % args),
    )

    status = run_worker(app)

    assert any("backlog warning before startup reconcile" in warning for warning in captured["warnings"])
    assert any("backlog warning after startup reconcile" in warning for warning in captured["warnings"])
    assert captured["worked"] is True
    assert captured["closed"] is True
    assert status == 0


def test_emit_backlog_operational_alert_uses_dispatcher(monkeypatch):
    captured = {}

    class FakeDispatcher:
        def notify_operational_issue(self, **payload):
            captured["payload"] = payload
            return True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "SCRAPE_QUEUE_NAME": "worker-q",
            "REDIS_URL": "redis://localhost:6379/3",
        }
    )
    settings = load_worker_runtime_settings(app)

    monkeypatch.setattr("services.worker_runtime.get_alert_dispatcher", lambda: FakeDispatcher())

    delivered = emit_backlog_operational_alert(
        {
            "queued_count": 3,
            "running_count": 1,
            "oldest_queued_age_seconds": 400,
            "oldest_running_age_seconds": 50,
        },
        ["queued_count>=2"],
        settings,
    )

    assert delivered is True
    assert captured["payload"]["event_type"] == "worker_backlog_warning"
    assert captured["payload"]["component"] == "worker_runtime"
    assert captured["payload"]["details"]["queue_name"] == "worker-q"


def test_run_worker_emits_operational_alert_for_unhealthy_backlog(monkeypatch):
    captured = {"alerts": []}

    class FakeRedis:
        @staticmethod
        def from_url(redis_url):
            return FakeRedis()

    class FakeQueue:
        def __init__(self, name, connection):
            pass

    class FakeWorker:
        def __init__(self, queues, connection):
            pass

        def work(self, burst, with_scheduler):
            captured["worked"] = True

    class FakeDispatcher:
        def notify_operational_issue(self, **payload):
            captured["alerts"].append(payload)
            return True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WARM_BROWSER_POOL": False,
            "WORKER_BACKLOG_WARN_COUNT": 2,
            "WORKER_BACKLOG_WARN_AGE_SECONDS": 300,
        }
    )

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr("services.worker_runtime.import_rq_queue", lambda: FakeQueue)
    monkeypatch.setattr("services.worker_runtime.import_rq_simple_worker", lambda: FakeWorker)
    monkeypatch.setattr("services.worker_runtime.ensure_additive_schema_ready", lambda: {"ready": True, "blockers": []})
    monkeypatch.setattr(
        "services.worker_runtime.get_job_backlog_snapshot",
        lambda: {
            "queued_count": 3,
            "running_count": 1,
            "oldest_queued_age_seconds": 400,
            "oldest_running_age_seconds": 50,
        },
    )
    monkeypatch.setattr("services.worker_runtime.reconcile_stalled_jobs", lambda: [])
    monkeypatch.setattr("services.worker_runtime.get_alert_dispatcher", lambda: FakeDispatcher())
    monkeypatch.setattr("services.worker_runtime.close_browser_pool", lambda: captured.setdefault("closed", True))

    status = run_worker(app)

    assert len(captured["alerts"]) == 1
    assert captured["alerts"][0]["event_type"] == "worker_backlog_warning"
    assert captured["worked"] is True
    assert captured["closed"] is True
    assert status == 0


def test_get_worker_health_snapshot_reports_rq_state(monkeypatch):
    class FakeRedis:
        @staticmethod
        def from_url(redis_url):
            return FakeRedis()

        def ping(self):
            return True

    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "SCRAPE_QUEUE_NAME": "worker-q",
            "REDIS_URL": "redis://localhost:6379/9",
            "WORKER_BACKLOG_WARN_COUNT": 2,
            "WORKER_BACKLOG_WARN_AGE_SECONDS": 300,
        }
    )

    monkeypatch.setattr("services.worker_runtime.Redis", FakeRedis)
    monkeypatch.setattr(
        "services.worker_runtime.get_job_backlog_snapshot",
        lambda: {
            "queued_count": 3,
            "running_count": 1,
            "oldest_queued_age_seconds": 400,
            "oldest_running_age_seconds": 50,
        },
    )
    monkeypatch.setattr(
        "services.worker_runtime.get_browser_pool_health",
        lambda: {"runtimes": {}, "warm_sites": ["mercari"], "shared_runtime_default_enabled": True},
    )
    monkeypatch.setattr(
        "services.worker_runtime.get_alert_dispatcher",
        lambda: SimpleNamespace(operational_webhook_url="https://alerts.example.test/ops"),
    )

    snapshot = get_worker_health_snapshot(app)

    assert snapshot["worker_runtime_supported"] is True
    assert snapshot["redis_ok"] is True
    assert snapshot["queue_name"] == "worker-q"
    assert snapshot["backlog_issues"] == ["queued_count>=2", "oldest_queued_age_seconds>=300"]
    assert snapshot["operational_alert_enabled"] is True


def test_get_worker_health_snapshot_for_inmemory_skips_redis(monkeypatch):
    app = create_web_app(
        config_overrides={
            "TESTING": True,
            "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
            "SCRAPE_QUEUE_BACKEND": "inmemory",
        }
    )

    monkeypatch.setattr(
        "services.worker_runtime.get_job_backlog_snapshot",
        lambda: {"queued_count": 0, "running_count": 0},
    )
    monkeypatch.setattr(
        "services.worker_runtime.get_browser_pool_health",
        lambda: {"runtimes": {}, "warm_sites": [], "shared_runtime_default_enabled": False},
    )
    monkeypatch.setattr(
        "services.worker_runtime.get_alert_dispatcher",
        lambda: SimpleNamespace(operational_webhook_url=""),
    )

    snapshot = get_worker_health_snapshot(app)

    assert snapshot["worker_runtime_supported"] is False
    assert snapshot["queue_backend"] == "inmemory"
    assert snapshot["redis_ok"] is None
    assert snapshot["redis_error"] is None
