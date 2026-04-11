import worker


def test_worker_main_builds_worker_app_and_runs_runtime(monkeypatch):
    sentinel_app = object()
    captured = {}

    def fake_create_worker_app(config_overrides=None):
        captured["config_overrides"] = config_overrides
        return sentinel_app

    def fake_run_worker(app):
        captured["app"] = app
        return 0

    monkeypatch.setattr("worker.create_worker_app", fake_create_worker_app)
    monkeypatch.setattr("worker.run_worker", fake_run_worker)
    monkeypatch.delenv("MERCARI_USE_BROWSER_POOL_DETAIL", raising=False)
    monkeypatch.delenv("MERCARI_PATROL_USE_BROWSER_POOL", raising=False)
    monkeypatch.delenv("SNKRDUNK_USE_BROWSER_POOL_DYNAMIC", raising=False)

    assert worker.main() == 0
    assert captured["app"] is sentinel_app
    assert captured["config_overrides"]["SCRAPE_QUEUE_BACKEND"] == "rq"
    assert captured["config_overrides"]["WARM_BROWSER_POOL"] == "1"
    assert captured["config_overrides"]["WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP"] == "0"
    assert captured["config_overrides"]["WORKER_SELECTOR_REPAIR_LIMIT"] == "1"
    assert worker.os.environ["MERCARI_USE_BROWSER_POOL_DETAIL"] == "1"
    assert worker.os.environ["MERCARI_PATROL_USE_BROWSER_POOL"] == "1"
    assert worker.os.environ["SNKRDUNK_USE_BROWSER_POOL_DYNAMIC"] == "1"
    worker.os.environ.pop("MERCARI_USE_BROWSER_POOL_DETAIL", None)
    worker.os.environ.pop("MERCARI_PATROL_USE_BROWSER_POOL", None)
    worker.os.environ.pop("SNKRDUNK_USE_BROWSER_POOL_DYNAMIC", None)


def test_worker_main_passes_scheduler_flag(monkeypatch):
    sentinel_app = object()
    captured = {}

    def fake_create_worker_app(config_overrides=None):
        captured["config_overrides"] = config_overrides
        return sentinel_app

    monkeypatch.setattr("worker.create_worker_app", fake_create_worker_app)
    monkeypatch.setattr("worker.run_worker", lambda app: 0)
    monkeypatch.setenv("WORKER_ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", "0")
    monkeypatch.setenv("WORKER_SELECTOR_REPAIR_LIMIT", "3")

    assert worker.main() == 0
    assert captured["config_overrides"]["ENABLE_SCHEDULER"] == "1"
    assert captured["config_overrides"]["WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP"] == "0"
    assert captured["config_overrides"]["WORKER_SELECTOR_REPAIR_LIMIT"] == "3"
