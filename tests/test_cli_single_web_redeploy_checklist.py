import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_single_web_redeploy_checklist_with_authenticated_postdeploy_smoke():
    from cli import run_single_web_redeploy_checklist

    snapshot = run_single_web_redeploy_checklist(
        base_url="https://example.com",
        username="smoke",
        password="secret",
    )

    assert snapshot["ready"] is True
    assert snapshot["authenticated_smoke_configured"] is True
    assert snapshot["predeploy_commands"] == ["flask single-web-redeploy-readiness"]
    assert snapshot["postdeploy_commands"][0] == (
        "flask single-web-postdeploy-smoke --base-url https://example.com --retries 4 --retry-delay-seconds 2"
    )
    assert any(
        "--retries 4 --retry-delay-seconds 2 --username smoke --password <redacted> --ensure-user" in command
        for command in snapshot["postdeploy_commands"]
    )


def test_single_web_redeploy_checklist_cli_prints_json(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "single-web-redeploy-checklist",
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
    assert payload["runbook_path"] == "docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md"
