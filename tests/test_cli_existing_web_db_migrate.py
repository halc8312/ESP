import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_existing_web_db_migrate_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr("cli.run_alembic_upgrade_for_database_url", lambda destination_url: "head")
    monkeypatch.setattr(
        "cli.run_existing_web_database_migration",
        lambda **kwargs: {
            "ready": True,
            "mode": "dry-run",
            "source_url": kwargs["source_url"],
            "destination_url": kwargs["destination_url"],
            "blockers": [],
            "warnings": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "existing-web-db-migrate",
            "--destination-url",
            "postgresql+psycopg://esp:secret@example.com:5432/esp",
            "--dry-run",
            "--prepare-destination-schema",
        ]
    )

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["prepared_destination_schema"] == "alembic"


def test_existing_web_db_migrate_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_existing_web_database_migration",
        lambda **kwargs: {
            "ready": False,
            "mode": "verify-only",
            "blockers": ["row_count_mismatch:users:10:9"],
            "warnings": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "existing-web-db-migrate",
            "--destination-url",
            "postgresql+psycopg://esp:secret@example.com:5432/esp",
            "--verify-only",
        ]
    )

    assert result.exit_code == 1


def test_existing_web_db_migrate_cli_reports_schema_prepare_failure(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_alembic_upgrade_for_database_url",
        lambda destination_url: (_ for _ in ()).throw(RuntimeError("alembic failed")),
    )

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "existing-web-db-migrate",
            "--destination-url",
            "postgresql+psycopg://esp:secret@example.com:5432/esp",
            "--prepare-destination-schema",
        ]
    )

    assert result.exit_code == 1
    payload = _load_last_json_line(result.output)
    assert payload["blockers"] == ["destination_schema_prepare_failed"]
