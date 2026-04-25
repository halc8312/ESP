from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

from database import Base
from services.database_migration import run_existing_web_database_migration


def _create_legacy_source_db(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY,
                        username VARCHAR(100) NOT NULL,
                        password_hash VARCHAR(200) NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE scrape_jobs (
                        job_id VARCHAR(64) PRIMARY KEY,
                        status VARCHAR(32) NOT NULL,
                        site VARCHAR(32) NOT NULL,
                        mode VARCHAR(32) NOT NULL,
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
            connection.execute(
                text(
                    """
                    INSERT INTO users (id, username, password_hash)
                    VALUES (1, 'legacy-user', 'hashed-password')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO scrape_jobs (
                        job_id, status, site, mode, requested_by, request_payload,
                        result_summary, error_message, started_at, finished_at, created_at, updated_at
                    )
                    VALUES (
                        'job-1', 'completed', 'mercari', 'persist', 1, '{}',
                        '{"items_count": 1}', NULL,
                        '2026-04-01 00:00:00', '2026-04-01 00:01:00',
                        '2026-04-01 00:00:00', '2026-04-01 00:01:00'
                    )
                    """
                )
            )
    finally:
        engine.dispose()


def _create_target_db(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()


def _build_sqlite_urls() -> tuple[str, str]:
    tmp_dir = Path(__file__).resolve().parent / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    source_path = tmp_dir / f"migration_source_{uuid.uuid4().hex}.db"
    destination_path = tmp_dir / f"migration_target_{uuid.uuid4().hex}.db"
    return f"sqlite:///{source_path.as_posix()}", f"sqlite:///{destination_path.as_posix()}"


def test_existing_web_database_migration_dry_run_reports_plan_without_writing():
    source_url, destination_url = _build_sqlite_urls()
    _create_legacy_source_db(source_url)
    _create_target_db(destination_url)

    snapshot = run_existing_web_database_migration(
        source_url,
        destination_url,
        dry_run=True,
        table_names=("users", "scrape_jobs"),
        enforce_backend_contract=False,
    )

    assert snapshot["ready"] is True
    assert snapshot["mode"] == "dry-run"
    assert snapshot["table_results"][0]["source_count"] == 1
    assert any(
        warning.startswith("source_columns_missing:scrape_jobs:")
        and "context_payload" in warning
        for warning in snapshot["warnings"]
    )

    target_engine = create_engine(destination_url)
    try:
        with target_engine.connect() as connection:
            assert int(connection.execute(text('SELECT COUNT(*) FROM "users"')).scalar_one()) == 0
            assert int(connection.execute(text('SELECT COUNT(*) FROM "scrape_jobs"')).scalar_one()) == 0
    finally:
        target_engine.dispose()


def test_existing_web_database_migration_copies_legacy_sqlite_rows_and_verifies_counts():
    source_url, destination_url = _build_sqlite_urls()
    _create_legacy_source_db(source_url)
    _create_target_db(destination_url)

    snapshot = run_existing_web_database_migration(
        source_url,
        destination_url,
        table_names=("users", "scrape_jobs"),
        enforce_backend_contract=False,
    )

    assert snapshot["ready"] is True
    assert snapshot["mode"] == "migrate"
    assert snapshot["migration_error"] is None
    assert snapshot["table_results"][1]["copied_rows"] == 1

    target_engine = create_engine(destination_url)
    try:
        with target_engine.connect() as connection:
            assert int(connection.execute(text('SELECT COUNT(*) FROM "users"')).scalar_one()) == 1
            assert int(connection.execute(text('SELECT COUNT(*) FROM "scrape_jobs"')).scalar_one()) == 1
            context_payload = connection.execute(
                text('SELECT context_payload FROM "scrape_jobs" WHERE job_id = :job_id'),
                {"job_id": "job-1"},
            ).scalar_one()
            assert context_payload is None
    finally:
        target_engine.dispose()


def test_existing_web_database_migration_verify_only_flags_row_count_mismatch():
    source_url, destination_url = _build_sqlite_urls()
    _create_legacy_source_db(source_url)
    _create_target_db(destination_url)

    snapshot = run_existing_web_database_migration(
        source_url,
        destination_url,
        verify_only=True,
        table_names=("users", "scrape_jobs"),
        enforce_backend_contract=False,
    )

    assert snapshot["ready"] is False
    assert "row_count_mismatch:users:1:0" in snapshot["blockers"]
    assert "row_count_mismatch:scrape_jobs:1:0" in snapshot["blockers"]
