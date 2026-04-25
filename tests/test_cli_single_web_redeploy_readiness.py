import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_single_web_redeploy_readiness_aggregates_local_gate(monkeypatch, app):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "ready": True,
            "blockers": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_local_verification_suite",
        lambda current_app, **kwargs: {
            "ready": True,
            "profile": "parser",
            "steps": [{"name": "single-web-smoke-preview", "ready": True, "blockers": []}],
            "blockers": [],
        },
    )

    from cli import run_single_web_redeploy_readiness

    snapshot = run_single_web_redeploy_readiness(app, strict_parser=True)

    assert snapshot["ready"] is True
    assert snapshot["strict_parser"] is True
    assert [step["name"] for step in snapshot["steps"]] == [
        "single-web-predeploy",
        "single-web-local-verify-parser",
    ]
    assert snapshot["steps"][1]["profile"] == "parser"


def test_run_single_web_redeploy_readiness_fails_on_local_verify_blocker(monkeypatch, app):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "ready": True,
            "blockers": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_local_verification_suite",
        lambda current_app, **kwargs: {
            "ready": False,
            "profile": "parser",
            "steps": [],
            "blockers": ["schema-drift-check"],
        },
    )

    from cli import run_single_web_redeploy_readiness

    snapshot = run_single_web_redeploy_readiness(app)

    assert snapshot["ready"] is False
    assert snapshot["blockers"] == ["single-web-local-verify-parser"]


def test_single_web_redeploy_readiness_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_single_web_redeploy_readiness",
        lambda current_app, **kwargs: {
            "ready": True,
            "steps": [{"name": "single-web-predeploy", "ready": True, "blockers": []}],
            "blockers": [],
            "strict_parser": kwargs["strict_parser"],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["single-web-redeploy-readiness", "--strict-parser"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["strict_parser"] is True
