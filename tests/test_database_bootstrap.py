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
    assert "tracker_dismissed_at" in columns


def test_apply_additive_startup_migrations_avoids_missing_column_probe_queries(monkeypatch):
    class FakeInspector:
        def __init__(self, table_columns):
            self._table_columns = table_columns

        def get_table_names(self):
            return list(self._table_columns)

        def get_columns(self, table_name):
            return [{"name": name} for name in sorted(self._table_columns.get(table_name, set()))]

    class FakeConnection:
        class dialect:
            supports_savepoints = False

        def __init__(self):
            self.table_columns = {
                "scrape_jobs": {
                    "job_id",
                    "status",
                    "site",
                    "mode",
                    "requested_by",
                    "request_payload",
                    "result_summary",
                    "error_message",
                    "started_at",
                    "finished_at",
                    "created_at",
                    "updated_at",
                }
            }
            self.statements = []

        def execute(self, statement):
            sql = str(statement)
            self.statements.append(sql)
            if sql.lstrip().upper().startswith("SELECT "):
                raise AssertionError("missing-column probes should not use SELECT statements")
            if sql.startswith("ALTER TABLE scrape_jobs ADD COLUMN "):
                column_name = sql.split()[5]
                self.table_columns.setdefault("scrape_jobs", set()).add(column_name)

        def in_transaction(self):
            return False

        def commit(self):
            return None

        def rollback(self):
            return None

    fake_connection = FakeConnection()
    monkeypatch.setattr(database, "inspect", lambda connection: FakeInspector(connection.table_columns))

    results = database.apply_additive_startup_migrations(bind=fake_connection)

    assert "scrape_jobs.tracker_dismissed_at" in results["applied"]
    assert all(not statement.lstrip().upper().startswith("SELECT ") for statement in fake_connection.statements)


def test_run_alembic_upgrade_for_database_url_backfills_tracker_dismissed_at_from_0003():
    smoke_db = Path(f"test_db_alembic_upgrade_{uuid.uuid4().hex}.sqlite")
    smoke_db_url = f"sqlite:///{smoke_db.resolve().as_posix()}"
    smoke_engine = database.create_app_engine(smoke_db_url)

    try:
        with smoke_engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260329_0003')"))
            connection.execute(
                text(
                    """
                    CREATE TABLE scrape_jobs (
                        job_id VARCHAR(64) PRIMARY KEY,
                        logical_job_id VARCHAR(64),
                        parent_job_id VARCHAR(64),
                        status VARCHAR(32),
                        site VARCHAR(32),
                        mode VARCHAR(32),
                        requested_by INTEGER,
                        request_payload TEXT,
                        context_payload TEXT,
                        progress_current INTEGER,
                        progress_total INTEGER,
                        result_summary TEXT,
                        result_payload TEXT,
                        error_message TEXT,
                        error_payload TEXT,
                        started_at TIMESTAMP,
                        finished_at TIMESTAMP,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    )
                    """
                )
            )

        database.run_alembic_upgrade_for_database_url(smoke_db_url)

        upgraded_engine = database.create_app_engine(smoke_db_url)
        try:
            columns = {column["name"] for column in inspect(upgraded_engine).get_columns("scrape_jobs")}
            indexes = {index["name"] for index in inspect(upgraded_engine).get_indexes("scrape_jobs")}
            with upgraded_engine.connect() as connection:
                version_num = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "tracker_dismissed_at" in columns
    assert "ix_scrape_jobs_tracker_dismissed_at" in indexes
    assert version_num == "20260411_0004"


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
    assert "scrape_jobs.tracker_dismissed_at" in snapshot["missing_columns"]
    assert "scrape_job_events" in snapshot["missing_tables"]
