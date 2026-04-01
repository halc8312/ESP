import json

from flask import Flask


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_render_cutover_readiness_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_cutover_readiness",
        lambda current_app, **kwargs: {
            "ready": True,
            "steps": [{"name": "split-render-predeploy", "ready": True, "blockers": []}],
            "blockers": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-cutover-readiness"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True


def test_render_cutover_readiness_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_cutover_readiness",
        lambda current_app, **kwargs: {
            "ready": False,
            "steps": [{"name": "split-render-predeploy", "ready": False, "blockers": ["redis_connection_failed"]}],
            "blockers": ["split-render-predeploy"],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-cutover-readiness", "--strict"])

    assert result.exit_code == 1


def test_run_render_cutover_readiness_keeps_current_single_web_contract(monkeypatch):
    from cli import run_render_cutover_readiness

    observed = {}
    current_app = Flask(__name__)
    current_app.config["TESTING"] = False

    def fake_create_web_app(config_overrides=None):
        app = Flask("fake-web")
        app.config.update(config_overrides or {})
        return app

    def fake_create_worker_app(config_overrides=None):
        app = Flask("fake-worker")
        app.config.update(config_overrides or {})
        return app

    def fake_build_predeploy_snapshot(app, target="single-web"):
        return {
            "ready": True,
            "blockers": [],
            "warnings": [],
            "target": target,
            "queue_backend": app.config.get("SCRAPE_QUEUE_BACKEND"),
        }

    def fake_worker_health_snapshot(app):
        return {
            "queue_backend": app.config.get("SCRAPE_QUEUE_BACKEND"),
            "redis_ok": True,
            "redis_error": None,
            "backlog_issues": [],
        }

    def fake_local_verify(app, **kwargs):
        observed["queue_backend"] = app.config.get("SCRAPE_QUEUE_BACKEND")
        observed["web_scheduler_mode"] = app.config.get("WEB_SCHEDULER_MODE")
        return {"ready": True, "blockers": [], "profile": kwargs["profile"], "steps": []}

    monkeypatch.setattr("app.create_web_app", fake_create_web_app)
    monkeypatch.setattr("app.create_worker_app", fake_create_worker_app)
    monkeypatch.setattr("cli.build_predeploy_snapshot", fake_build_predeploy_snapshot)
    monkeypatch.setattr("cli.get_worker_health_snapshot", fake_worker_health_snapshot)
    monkeypatch.setattr(
        "cli.run_render_blueprint_audit",
        lambda path="render.yaml": {"ready": True, "blockers": [], "warnings": []},
    )
    monkeypatch.setattr(
        "cli.run_render_budget_guardrail_audit",
        lambda path="render.yaml": {
            "ready": True,
            "blockers": [],
            "warnings": [],
            "estimated_monthly_core_usd": 61,
        },
    )
    monkeypatch.setattr(
        "cli.inspect_additive_schema_drift",
        lambda: {"ready": True, "blockers": []},
    )
    monkeypatch.setattr("cli.run_local_verification_suite", fake_local_verify)

    snapshot = run_render_cutover_readiness(current_app, strict=True)

    assert snapshot["ready"] is True
    assert observed["queue_backend"] == "inmemory"
    assert observed["web_scheduler_mode"] == "auto"
