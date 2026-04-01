import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_schema_drift_check_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.inspect_additive_schema_drift",
        lambda: {
            "ready": True,
            "blockers": [],
            "missing_tables": [],
            "missing_columns": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["schema-drift-check"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True


def test_schema_drift_check_cli_fails_on_missing_columns(app, monkeypatch):
    monkeypatch.setattr(
        "cli.inspect_additive_schema_drift",
        lambda: {
            "ready": False,
            "blockers": ["scrape_jobs.context_payload"],
            "missing_tables": [],
            "missing_columns": ["scrape_jobs.context_payload"],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["schema-drift-check"])

    assert result.exit_code == 1
