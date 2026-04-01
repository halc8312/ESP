import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_single_web_postdeploy_smoke_uses_current_single_web_expectations(monkeypatch):
    captured = {}

    def fake_render_postdeploy(base_url, **kwargs):
        captured["base_url"] = base_url
        captured["kwargs"] = kwargs
        return {
            "ready": True,
            "blockers": [],
            "base_url": base_url,
            "expect_queue_backend": kwargs.get("expect_queue_backend"),
            "expect_runtime_role": kwargs.get("expect_runtime_role"),
            "expect_scheduler_mode": kwargs.get("expect_scheduler_mode"),
        }

    monkeypatch.setattr("cli.run_render_postdeploy_smoke", fake_render_postdeploy)

    from cli import run_single_web_postdeploy_smoke

    snapshot = run_single_web_postdeploy_smoke(
        "https://example.com",
        retries=4,
        retry_delay_seconds=2.5,
        username="smoke",
        password="secret",
        ensure_user=True,
    )

    assert snapshot["ready"] is True
    assert snapshot["runbook_path"] == "docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md"
    assert captured["base_url"] == "https://example.com"
    assert captured["kwargs"]["expect_queue_backend"] == "inmemory"
    assert captured["kwargs"]["expect_runtime_role"] == "web"
    assert captured["kwargs"]["expect_scheduler_mode"] == "enabled"
    assert captured["kwargs"]["retries"] == 4
    assert captured["kwargs"]["retry_delay_seconds"] == 2.5
    assert captured["kwargs"]["username"] == "smoke"
    assert captured["kwargs"]["password"] == "secret"
    assert captured["kwargs"]["ensure_user"] is True


def test_single_web_postdeploy_smoke_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_single_web_postdeploy_smoke",
        lambda base_url, **kwargs: {
            "ready": True,
            "blockers": [],
            "base_url": base_url,
            "runbook_path": "docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md",
            "retry_policy": {"retries": kwargs.get("retries"), "retry_delay_seconds": kwargs.get("retry_delay_seconds")},
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["single-web-postdeploy-smoke", "--base-url", "https://example.com"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["base_url"] == "https://example.com"
    assert payload["retry_policy"]["retries"] == 2
