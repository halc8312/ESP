from pathlib import Path
import uuid

import pytest
from sqlalchemy import inspect, text

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


def test_get_database_url_normalizes_legacy_postgres_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:secret@example.com/app")

    assert database.get_database_url() == "postgresql://user:secret@example.com/app"
    assert database.get_database_backend() == "postgresql"
    assert "***" in database.redact_database_url()


def test_create_app_engine_normalizes_legacy_postgres_scheme(monkeypatch):
    captured = {}

    def fake_create_engine(url, echo=False):
        captured["url"] = url

        class FakeEngine:
            url = type("FakeUrl", (), {"drivername": "postgresql"})()

        return FakeEngine()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)

    engine = database.create_app_engine("postgres://user:secret@example.com/app")

    assert captured["url"] == "postgresql://user:secret@example.com/app"
    assert engine.url.drivername == "postgresql"


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


def test_apply_additive_startup_migrations_backfills_scrape_job_columns():
    smoke_db = Path(f"test_db_patchset_{uuid.uuid4().hex}.sqlite")
    smoke_engine = database.create_app_engine(f"sqlite:///{smoke_db.as_posix()}")

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE scrape_jobs (
                        job_id VARCHAR(64) PRIMARY KEY,
                        status VARCHAR(32),
                        site VARCHAR(32),
                        mode VARCHAR(32),
                        requested_by INTEGER,
                        request_payload TEXT,
                        result_summary TEXT,
                        error_message TEXT,
                        started_at TIMESTAMP,
                        finished_at TIMESTAMP,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    )
                    """
                )
            )

        with smoke_engine.begin() as connection:
            results = database.apply_additive_startup_migrations(bind=connection)
        columns = {column["name"] for column in inspect(smoke_engine).get_columns("scrape_jobs")}
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "scrape_jobs.context_payload" in results["applied"]
    assert "logical_job_id" in columns
    assert "parent_job_id" in columns
    assert "context_payload" in columns
    assert "progress_current" in columns
    assert "progress_total" in columns
    assert "result_payload" in columns
    assert "error_payload" in columns


def test_inspect_additive_schema_drift_reports_missing_scrape_job_columns():
    smoke_db = Path(f"test_db_drift_{uuid.uuid4().hex}.sqlite")
    smoke_engine = database.create_app_engine(f"sqlite:///{smoke_db.as_posix()}")

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE scrape_jobs (
                        job_id VARCHAR(64) PRIMARY KEY,
                        status VARCHAR(32),
                        site VARCHAR(32),
                        mode VARCHAR(32),
                        requested_by INTEGER,
                        request_payload TEXT,
                        result_summary TEXT,
                        error_message TEXT,
                        started_at TIMESTAMP,
                        finished_at TIMESTAMP,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    )
                    """
                )
            )

            snapshot = database.inspect_additive_schema_drift(bind=connection)
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert snapshot["ready"] is False
    assert "scrape_jobs.context_payload" in snapshot["missing_columns"]
    assert "scrape_job_events" in snapshot["missing_tables"]
