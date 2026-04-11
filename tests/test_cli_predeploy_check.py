import json

from cli import build_predeploy_snapshot


def test_predeploy_check_single_web_reports_ready(app, monkeypatch):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "blockers": [],
            "warnings": [],
            "ready": True,
        },
    )
    monkeypatch.setattr(
        "cli.inspect_additive_schema_drift",
        lambda: {
            "ready": True,
            "blockers": [],
            "missing_tables": [],
            "missing_columns": [],
            "database_backend": "sqlite",
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["predeploy-check"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["target"] == "single-web"
    assert payload["ready"] is True
    assert payload["schema_drift"]["ready"] is True


def test_predeploy_check_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "blockers": ["split_render_requires_postgresql_database"],
            "warnings": [],
            "ready": False,
        },
    )
    monkeypatch.setattr(
        "cli.inspect_additive_schema_drift",
        lambda: {
            "ready": True,
            "blockers": [],
            "missing_tables": [],
            "missing_columns": [],
            "database_backend": "sqlite",
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["predeploy-check", "--target", "split-render"])

    assert result.exit_code == 1


def test_predeploy_check_strict_fails_on_warning(app, monkeypatch):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "blockers": [],
            "warnings": ["redis_url_points_to_localhost"],
            "ready": True,
        },
    )
    monkeypatch.setattr(
        "cli.inspect_additive_schema_drift",
        lambda: {
            "ready": True,
            "blockers": [],
            "missing_tables": [],
            "missing_columns": [],
            "database_backend": "sqlite",
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["predeploy-check", "--target", "split-render", "--strict"])

    assert result.exit_code == 1


def test_predeploy_check_fails_on_schema_drift(app, monkeypatch):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "blockers": [],
            "warnings": [],
            "ready": True,
        },
    )
    monkeypatch.setattr(
        "cli.inspect_additive_schema_drift",
        lambda: {
            "ready": False,
            "blockers": ["missing_column:scrape_jobs.context_payload"],
            "missing_tables": [],
            "missing_columns": ["scrape_jobs.context_payload"],
            "database_backend": "sqlite",
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["predeploy-check", "--target", "single-web"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_drift"]["blockers"] == ["missing_column:scrape_jobs.context_payload"]
    assert "missing_column:scrape_jobs.context_payload" in payload["blockers"]


def test_build_predeploy_snapshot_blocks_rq_for_single_web(app):
    app.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )

    snapshot = build_predeploy_snapshot(app, target="single-web")

    assert "single_web_requires_inmemory_queue" in snapshot["blockers"]


def test_build_predeploy_snapshot_blocks_non_postgres_split_render(app):
    app.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WEB_SCHEDULER_MODE": "disabled",
            "ENABLE_SCHEDULER": False,
            "SCHEMA_BOOTSTRAP_MODE": "legacy",
        }
    )

    snapshot = build_predeploy_snapshot(app, target="split-render")

    assert "split_render_requires_postgresql_database" in snapshot["blockers"]
    assert "split_render_requires_alembic_schema_bootstrap" in snapshot["blockers"]


def test_build_predeploy_snapshot_blocks_selector_repair_startup_without_canaries(app, monkeypatch):
    monkeypatch.setattr(
        "cli.describe_schema_bootstrap",
        lambda mode: {
            "database_backend": "postgresql",
            "effective_mode": "alembic",
            "database_url": "postgresql://example.test/esp",
        },
    )
    app.config.update(
        {
            "SECRET_KEY": "not-dev-secret",
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WEB_SCHEDULER_MODE": "disabled",
            "ENABLE_SCHEDULER": False,
            "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": "1",
            "WORKER_SELECTOR_REPAIR_LIMIT": "1",
            "SELECTOR_REPAIR_MIN_SCORE": "90",
            "SELECTOR_REPAIR_MIN_CANARIES": "2",
            "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL": "",
            "SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL": "",
        }
    )

    snapshot = build_predeploy_snapshot(app, target="split-render")

    assert "selector_repair_canaries_missing:mercari_detail" in snapshot["blockers"]
    assert "selector_repair_canaries_missing:snkrdunk_detail" in snapshot["blockers"]
