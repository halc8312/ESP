import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_render_budget_guardrail_audit_matches_repo_default_guardrail():
    from cli import run_render_budget_guardrail_audit

    snapshot = run_render_budget_guardrail_audit("render.yaml")

    assert snapshot["ready"] is True
    assert snapshot["budget_guardrail_usd"] == 80
    assert snapshot["estimated_monthly_core_usd"] == 61
    assert snapshot["plan_snapshot"]["esp-web"]["actual_plan"] == "starter"
    assert snapshot["plan_snapshot"]["esp-worker"]["actual_plan"] == "standard"
    assert snapshot["plan_snapshot"]["esp-postgres"]["actual_plan"] == "basic-1gb"


def test_render_budget_guardrail_audit_cli_prints_json(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-budget-guardrail-audit", "--blueprint-path", "render.yaml"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["estimated_monthly_core_usd"] == 61
