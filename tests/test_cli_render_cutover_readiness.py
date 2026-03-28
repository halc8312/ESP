import json


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
