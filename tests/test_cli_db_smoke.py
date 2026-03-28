import json


def test_db_smoke_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_database_smoke_check",
        lambda **kwargs: {
            "database_backend": "sqlite",
            "blockers": [],
            "ready": True,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["db-smoke"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["database_backend"] == "sqlite"
    assert payload["ready"] is True


def test_db_smoke_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_database_smoke_check",
        lambda **kwargs: {
            "database_backend": "sqlite",
            "blockers": ["database_backend_mismatch:sqlite"],
            "ready": False,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["db-smoke", "--require-backend", "postgresql"])

    assert result.exit_code == 1
