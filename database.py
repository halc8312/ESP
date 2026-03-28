from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker


DEFAULT_DATABASE_URL = "sqlite:///mercari.db"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_app_engine(database_url: str | None = None):
    resolved_url = database_url or get_database_url()
    print(f"DEBUG: Using database URL: {resolved_url}")
    engine = create_engine(resolved_url, echo=False)

    if "sqlite" in engine.url.drivername:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))

    return engine


engine = create_app_engine()
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def init_db(bind=None):
    Base.metadata.create_all(bind or engine)


def alembic_available() -> bool:
    return bool(importlib.util.find_spec("alembic"))


def get_database_backend(database_url: str | None = None) -> str:
    resolved_url = database_url or get_database_url()
    try:
        driver_name = make_url(resolved_url).drivername.lower()
    except Exception:
        return "unknown"

    if "sqlite" in driver_name:
        return "sqlite"
    if driver_name.startswith("postgresql") or driver_name.startswith("postgres"):
        return "postgresql"
    if "mysql" in driver_name:
        return "mysql"
    return driver_name


def redact_database_url(database_url: str | None = None) -> str:
    resolved_url = database_url or get_database_url()
    try:
        return make_url(resolved_url).render_as_string(hide_password=True)
    except Exception:
        return resolved_url


def describe_schema_bootstrap(schema_mode: str = "auto", config_path: str = "alembic.ini") -> dict[str, str | bool | None]:
    normalized_mode = str(schema_mode or "auto").strip().lower()
    config_present = Path(config_path).exists()
    dependency_present = alembic_available()

    if normalized_mode not in {"auto", "alembic", "legacy", "disabled"}:
        raise ValueError(f"Unsupported schema bootstrap mode: {schema_mode}")

    if normalized_mode == "disabled":
        effective_mode = "disabled"
        fallback_reason = None
    elif normalized_mode == "legacy":
        effective_mode = "legacy"
        fallback_reason = None
    elif normalized_mode == "alembic":
        if not config_present:
            effective_mode = "error"
            fallback_reason = "alembic_config_missing"
        elif not dependency_present:
            effective_mode = "error"
            fallback_reason = "alembic_dependency_missing"
        else:
            effective_mode = "alembic"
            fallback_reason = None
    elif config_present and dependency_present:
        effective_mode = "alembic"
        fallback_reason = None
    elif config_present:
        effective_mode = "legacy"
        fallback_reason = "alembic_dependency_missing"
    else:
        effective_mode = "legacy"
        fallback_reason = "alembic_config_missing"

    return {
        "requested_mode": normalized_mode,
        "effective_mode": effective_mode,
        "alembic_config_present": config_present,
        "alembic_dependency_present": dependency_present,
        "fallback_reason": fallback_reason,
        "database_backend": get_database_backend(),
        "database_url": redact_database_url(),
    }


def run_alembic_upgrade(revision: str = "head", config_path: str = "alembic.ini") -> str:
    if not alembic_available():
        raise RuntimeError("Alembic is not installed")

    from alembic import command
    from alembic.config import Config

    config = Config(config_path)
    config.set_main_option("sqlalchemy.url", get_database_url())
    command.upgrade(config, revision)
    return revision


def bootstrap_schema(schema_mode: str = "auto") -> str:
    description = describe_schema_bootstrap(schema_mode)
    effective_mode = str(description["effective_mode"])

    if effective_mode == "disabled":
        return "disabled"
    if effective_mode == "error":
        fallback_reason = description.get("fallback_reason")
        if fallback_reason == "alembic_config_missing":
            raise RuntimeError("Alembic bootstrap requested but alembic.ini was not found")
        raise RuntimeError("Alembic bootstrap requested but Alembic is not installed")
    if effective_mode == "alembic":
        run_alembic_upgrade()
        return "alembic"

    init_db()
    return "legacy"


def run_database_smoke_check(
    database_url: str | None = None,
    *,
    require_backend: str | None = None,
    apply_migrations: bool = False,
    schema_mode: str = "auto",
    expected_tables: tuple[str, ...] = ("users", "products", "scrape_jobs"),
) -> dict[str, object]:
    resolved_url = database_url or get_database_url()
    backend = get_database_backend(resolved_url)
    schema = describe_schema_bootstrap(schema_mode)

    if apply_migrations:
        applied_schema_mode = bootstrap_schema(schema_mode)
        schema = describe_schema_bootstrap(applied_schema_mode)

    connect_ok = False
    roundtrip_ok = False
    roundtrip_count = None
    smoke_error = None

    smoke_engine = create_app_engine(resolved_url)
    try:
        with smoke_engine.begin() as connection:
            connect_ok = connection.execute(text("SELECT 1")).scalar_one() == 1

            temp_table = f"esp_smoke_{uuid.uuid4().hex[:12]}"
            connection.execute(text(f"CREATE TEMP TABLE {temp_table} (id INTEGER)"))
            connection.execute(text(f"INSERT INTO {temp_table} (id) VALUES (1)"))
            roundtrip_count = connection.execute(text(f"SELECT COUNT(*) FROM {temp_table}")).scalar_one()
            roundtrip_ok = roundtrip_count == 1
    except Exception as exc:
        smoke_error = str(exc)

    table_names: list[str] = []
    try:
        inspector = inspect(smoke_engine)
        table_names = sorted(inspector.get_table_names())
    except Exception as exc:
        if smoke_error is None:
            smoke_error = str(exc)
    missing_tables = [table for table in expected_tables if table not in table_names]

    blockers: list[str] = []
    if require_backend and backend != require_backend:
        blockers.append(f"database_backend_mismatch:{backend}")
    if not connect_ok:
        blockers.append("database_connection_failed")
    if connect_ok and not roundtrip_ok:
        blockers.append("database_roundtrip_failed")
    if missing_tables:
        blockers.append("expected_tables_missing")

    smoke_engine.dispose()

    return {
        "database_backend": backend,
        "database_url": redact_database_url(resolved_url),
        "schema": schema,
        "apply_migrations": apply_migrations,
        "connect_ok": connect_ok,
        "roundtrip_ok": roundtrip_ok,
        "roundtrip_count": roundtrip_count,
        "expected_tables": list(expected_tables),
        "missing_tables": missing_tables,
        "table_count": len(table_names),
        "blockers": blockers,
        "ready": not blockers,
        "error": smoke_error,
    }
