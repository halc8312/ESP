import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_render_cutover_brief_aggregates_operator_sections(app, monkeypatch):
    from cli import run_render_cutover_brief

    monkeypatch.setattr(
        "cli.run_render_budget_guardrail_audit",
        lambda blueprint_path="render.yaml": {"ready": True, "blockers": [], "warnings": [], "estimated_monthly_core_usd": 61},
    )
    monkeypatch.setattr(
        "cli.run_render_dashboard_inputs",
        lambda blueprint_path="render.yaml": {"ready": True, "blockers": [], "warnings": [], "service_inputs": []},
    )
    monkeypatch.setattr(
        "cli.run_render_worker_postdeploy_checklist",
        lambda blueprint_path="render.yaml": {"ready": True, "blockers": [], "warnings": [], "service_name": "esp-worker"},
    )
    monkeypatch.setattr(
        "cli.run_render_local_split_readiness",
        lambda current_app, **kwargs: {"ready": True, "blockers": [], "warnings": [], "steps": []},
    )
    monkeypatch.setattr(
        "cli.run_render_cutover_checklist",
        lambda **kwargs: {"ready": True, "blockers": [], "warnings": [], "pre_cutover_commands": []},
    )

    snapshot = run_render_cutover_brief(
        app,
        blueprint_path="render.yaml",
        base_url="https://example.com",
        username="smoke",
        password="secret",
    )

    assert snapshot["ready"] is True
    assert snapshot["authenticated_smoke_configured"] is True
    assert snapshot["budget_guardrail"]["estimated_monthly_core_usd"] == 61
    assert snapshot["worker_postdeploy_checklist"]["service_name"] == "esp-worker"


def test_render_cutover_brief_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_cutover_brief",
        lambda current_app, **kwargs: {"ready": True, "blockers": [], "warnings": []},
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-cutover-brief"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
