import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_render_dashboard_inputs_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_dashboard_inputs",
        lambda blueprint_path="render.yaml": {
            "ready": True,
            "blockers": [],
            "warnings": [],
            "service_inputs": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-dashboard-inputs"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True


def test_render_dashboard_inputs_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_dashboard_inputs",
        lambda blueprint_path="render.yaml": {
            "ready": False,
            "blockers": ["missing_service:esp-web"],
            "warnings": [],
            "service_inputs": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-dashboard-inputs"])

    assert result.exit_code == 1
