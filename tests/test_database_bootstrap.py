from pathlib import Path
import uuid

import pytest

import database
from models import ScrapeJob, ScrapeJobEvent


def test_bootstrap_schema_auto_falls_back_to_legacy(monkeypatch):
    calls = []

    monkeypatch.setattr(database, "alembic_available", lambda: False)
    monkeypatch.setattr(database, "init_db", lambda bind=None: calls.append(("legacy", bind)))

    applied_mode = database.bootstrap_schema("auto")

    assert applied_mode == "legacy"
    assert calls == [("legacy", None)]


def test_bootstrap_schema_alembic_requires_dependency(monkeypatch):
    monkeypatch.setattr(database, "alembic_available", lambda: False)

    with pytest.raises(RuntimeError, match="Alembic bootstrap requested"):
        database.bootstrap_schema("alembic")


def test_bootstrap_schema_alembic_requires_config(monkeypatch):
    monkeypatch.setattr(database, "alembic_available", lambda: True)
    monkeypatch.setattr(database.Path, "exists", lambda self: False)

    with pytest.raises(RuntimeError, match="alembic.ini was not found"):
        database.bootstrap_schema("alembic")


def test_describe_schema_bootstrap_reports_postgres_legacy_fallback(monkeypatch):
    monkeypatch.setattr(database, "alembic_available", lambda: False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@example.com/app")

    snapshot = database.describe_schema_bootstrap("auto")

    assert snapshot["effective_mode"] == "legacy"
    assert snapshot["fallback_reason"] == "alembic_dependency_missing"
    assert snapshot["database_backend"] == "postgresql"
    assert "secret" not in snapshot["database_url"]
    assert "***" in snapshot["database_url"]


def test_run_database_smoke_check_with_migrations(monkeypatch):
    smoke_db = Path(f"test_db_smoke_{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{smoke_db.as_posix()}")

    try:
        snapshot = database.run_database_smoke_check(
            require_backend="sqlite",
            apply_migrations=True,
            schema_mode="auto",
        )
    finally:
        smoke_db.unlink(missing_ok=True)

    assert snapshot["ready"] is True
    assert snapshot["connect_ok"] is True
    assert snapshot["roundtrip_ok"] is True
    assert snapshot["missing_tables"] == []


def test_run_database_smoke_check_reports_backend_mismatch(monkeypatch):
    smoke_db = Path(f"test_db_smoke_mismatch_{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{smoke_db.as_posix()}")

    try:
        snapshot = database.run_database_smoke_check(
            require_backend="postgresql",
            apply_migrations=False,
            schema_mode="disabled",
            expected_tables=(),
        )
    finally:
        smoke_db.unlink(missing_ok=True)

    assert snapshot["ready"] is False
    assert "database_backend_mismatch:sqlite" in snapshot["blockers"]


def test_scrape_job_tables_registered_in_metadata():
    assert ScrapeJob.__tablename__ in database.Base.metadata.tables
    assert ScrapeJobEvent.__tablename__ in database.Base.metadata.tables
