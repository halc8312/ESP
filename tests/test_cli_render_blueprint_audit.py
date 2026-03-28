import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_render_blueprint_audit_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_blueprint_audit",
        lambda blueprint_path="render.yaml": {
            "ready": True,
            "blockers": [],
            "warnings": [],
            "blueprint_path": blueprint_path,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-blueprint-audit"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True


def test_render_blueprint_audit_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_blueprint_audit",
        lambda blueprint_path="render.yaml": {
            "ready": False,
            "blockers": ["missing_service:esp-worker"],
            "warnings": [],
            "blueprint_path": blueprint_path,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-blueprint-audit"])

    assert result.exit_code == 1


def test_run_render_blueprint_audit_current_blueprint_is_ready():
    from cli import run_render_blueprint_audit

    snapshot = run_render_blueprint_audit("render.yaml")

    assert snapshot["ready"] is True
    assert snapshot["blockers"] == []
    assert "esp-web" in snapshot["service_names"]
    assert "esp-worker" in snapshot["service_names"]
    assert "esp-keyvalue" in snapshot["service_names"]
    assert "esp-postgres" in snapshot["database_names"]


def test_run_render_dashboard_inputs_current_blueprint_contains_manual_and_managed_envs():
    from cli import run_render_dashboard_inputs

    snapshot = run_render_dashboard_inputs("render.yaml")

    assert snapshot["ready"] is True
    assert snapshot["blockers"] == []
    web_service = next(service for service in snapshot["service_inputs"] if service["service"] == "esp-web")
    worker_service = next(service for service in snapshot["service_inputs"] if service["service"] == "esp-worker")
    assert any(env["key"] == "SECRET_KEY" for env in web_service["manual_envs"])
    assert any(env["key"] == "DATABASE_URL" for env in web_service["managed_envs"])
    assert any(env["key"] == "SCRAPE_QUEUE_BACKEND" for env in worker_service["fixed_envs"])
