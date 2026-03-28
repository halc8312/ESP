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

    runner = app.test_cli_runner()
    result = runner.invoke(args=["predeploy-check"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["target"] == "single-web"
    assert payload["ready"] is True


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

    runner = app.test_cli_runner()
    result = runner.invoke(args=["predeploy-check", "--target", "split-render", "--strict"])

    assert result.exit_code == 1


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
