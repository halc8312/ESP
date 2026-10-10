from types import SimpleNamespace

from flask import Flask

from services import worker_runtime


def test_startup_uses_same_batch_budget_config_as_scheduled_recovery(monkeypatch):
    app = Flask("startup-recovery")
    app.config.update(TESTING=True, RECOVERY_BATCH_TIMEOUT_SECONDS=17,
        RECOVERY_TOTAL_TIMEOUT_SECONDS=2, RECOVERY_PHASE_TIMEOUT_SECONDS=8)
    captured = {}

    def capture(_phases, **kwargs):
        captured.update(kwargs)
        return {"status": "ok", "features": {}}

    monkeypatch.setattr(worker_runtime, "run_recovery_phases", capture)
    worker_runtime.recover_features_on_startup(app, include_translation=False)
    assert captured["total_timeout_seconds"] == 17
    assert captured["phase_timeout_seconds"] == 8
    assert captured["phase_heartbeat_reserve_seconds"] == 0


def test_production_startup_reserves_heartbeat_time_and_caps_writer_deadline(monkeypatch):
    app = Flask("owned-startup-recovery")
    app.config.update(TESTING=False, SCRAPE_QUEUE_BACKEND="rq",
        RECOVERY_BATCH_TIMEOUT_SECONDS=17)
    captured = {}

    def capture(_phases, **kwargs):
        captured.update(kwargs)
        kwargs["heartbeat_writer"](event="test")
        return {"status": "ok", "features": {}}

    monkeypatch.setattr(worker_runtime, "run_recovery_phases", capture)
    monkeypatch.setattr(worker_runtime, "OwnedRecoveryExecutor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_runtime.time, "monotonic", lambda: 100.0)
    writes = []
    monkeypatch.setattr(worker_runtime, "_write_scheduler_heartbeat", lambda _app, **fields: writes.append(fields))
    worker_runtime.recover_features_on_startup(app, include_translation=False)
    assert captured["phase_heartbeat_reserve_seconds"] == 5
    assert writes == [{"_deadline": 117.0, "event": "test"}]


def test_startup_heartbeat_uncertainty_stops_admission_before_scheduler_exists(monkeypatch):
    app = Flask("candidate-startup-recovery")
    app.config.update(TESTING=False, SCRAPE_QUEUE_BACKEND="rq")
    calls = []

    def uncertain_heartbeat(_app, **_fields):
        # Match the production transport's fail-closed event transition.
        app.extensions["esp_scheduler_ownership_lost"].set()

    monkeypatch.setattr(worker_runtime, "_write_scheduler_heartbeat", uncertain_heartbeat)
    monkeypatch.setattr(worker_runtime, "OwnedRecoveryExecutor",
        lambda *_args, **_kwargs: lambda feature, _operation, _timeout: calls.append(feature))
    result = worker_runtime.recover_features_on_startup(app, include_translation=False)
    assert calls == []
    assert result["status"] == "degraded"
    assert result["features"]["thumbnail"]["error_type"] == "RecoveryCancelled"
    assert result["features"]["detail"]["error_type"] == "RecoveryCancelled"


def test_string_zero_supervision_flag_keeps_legacy_warm_and_skips_supervisor_close(monkeypatch):
    app = Flask("legacy-worker")
    app.config.update(PATROL_SUPERVISED_FETCH_ENABLED="0")
    settings = SimpleNamespace(reconcile_stalled_jobs_on_startup=False,
        warm_browser_pool=True, process_selector_repairs_on_startup=False,
        burst=False, with_scheduler=False)
    calls = []
    runtime = SimpleNamespace(settings=settings, worker=SimpleNamespace(work=lambda **kwargs: calls.append("work")))
    monkeypatch.setattr(worker_runtime, "ensure_additive_schema_ready", lambda: {})
    monkeypatch.setattr(worker_runtime, "build_worker_runtime", lambda _app: runtime)
    monkeypatch.setattr(worker_runtime, "start_worker_heartbeat", lambda _runtime: None)
    monkeypatch.setattr(worker_runtime, "stop_worker_heartbeat", lambda _handle: None)
    monkeypatch.setattr(worker_runtime, "get_job_backlog_snapshot", lambda: {})
    monkeypatch.setattr(worker_runtime, "evaluate_backlog_issues", lambda *_args: [])
    monkeypatch.setattr(worker_runtime, "recover_features_on_startup", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(worker_runtime, "warm_browser_pool", lambda: calls.append("warm"))
    monkeypatch.setattr(worker_runtime, "get_browser_pool_health", lambda: {"runtimes": {}})
    monkeypatch.setattr(worker_runtime, "close_browser_pool", lambda: calls.append("close_pool"))
    monkeypatch.setattr("services.patrol_supervisor.close_patrol_supervisor", lambda: calls.append("close_supervisor"))
    assert worker_runtime.run_worker(app) == 0
    assert calls == ["warm", "work", "close_pool"]
