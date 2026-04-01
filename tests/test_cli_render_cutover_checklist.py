import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_render_cutover_checklist_with_authenticated_postdeploy_smoke():
    from cli import run_render_cutover_checklist

    snapshot = run_render_cutover_checklist(
        blueprint_path="render.yaml",
        base_url="https://example.com",
        username="smoke",
        password="secret",
    )

    assert snapshot["ready"] is True
    assert snapshot["authenticated_smoke_configured"] is True
    assert snapshot["pre_cutover_commands"][1] == "flask schema-drift-check"
    assert snapshot["pre_cutover_commands"][2] == "flask render-blueprint-audit --blueprint-path render.yaml"
    assert snapshot["pre_cutover_commands"][3] == "flask render-budget-guardrail-audit --blueprint-path render.yaml"
    assert snapshot["pre_cutover_commands"][5] == "flask render-local-split-checklist --blueprint-path render.yaml"
    assert snapshot["postdeploy_commands"][0] == (
        "flask render-postdeploy-smoke --base-url https://example.com --retries 4 --retry-delay-seconds 2"
    )
    assert "flask render-worker-postdeploy-checklist --blueprint-path render.yaml" in snapshot["postdeploy_commands"]
    assert any(
        "--retries 4 --retry-delay-seconds 2 --username smoke --password <redacted> --ensure-user" in command
        for command in snapshot["postdeploy_commands"]
    )


def test_run_render_cutover_checklist_without_auth_uses_placeholder():
    from cli import run_render_cutover_checklist

    snapshot = run_render_cutover_checklist(
        blueprint_path="render.yaml",
        base_url="https://example.com",
    )

    assert snapshot["authenticated_smoke_configured"] is False
    assert any(
        "<smoke-user>" in command and "--retries 4 --retry-delay-seconds 2" in command and "--ensure-user" in command
        for command in snapshot["postdeploy_commands"]
    )


def test_render_cutover_checklist_cli_prints_json(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "render-cutover-checklist",
            "--base-url",
            "https://example.com",
            "--username",
            "smoke",
            "--password",
            "secret",
        ]
    )

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["base_url"] == "https://example.com"
