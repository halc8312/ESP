from __future__ import annotations

import importlib.util
import logging
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker


DEFAULT_DATABASE_URL = "sqlite:///mercari.db"
# Stable, application-specific 64-bit key used to serialize Alembic upgrades
# across independently starting web and worker processes.
ALEMBIC_UPGRADE_ADVISORY_LOCK_ID = 0x4553505F4D494752
logger = logging.getLogger("database")


def normalize_database_url(database_url: str | None = None) -> str:
    resolved_url = str(database_url or DEFAULT_DATABASE_URL).strip()
    if resolved_url.startswith("postgres://"):
        return "postgresql+psycopg://" + resolved_url[len("postgres://") :]
    if resolved_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + resolved_url[len("postgresql://") :]
    return resolved_url


def get_database_url() -> str:
    return normalize_database_url(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))


def create_app_engine(database_url: str | None = None):
    resolved_url = normalize_database_url(database_url or get_database_url())
    try:
        debug_url = make_url(resolved_url).render_as_string(hide_password=True)
    except Exception:
        debug_url = resolved_url
    logger.debug("Using database URL: %s", debug_url)

    is_sqlite = "sqlite" in resolved_url.lower()

    if is_sqlite:
        engine = create_engine(resolved_url, echo=False)
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
    else:
        engine = create_engine(
            resolved_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_recycle=300,
            pool_timeout=30,
        )

    return engine


engine = create_app_engine()
_session_factory = sessionmaker(bind=engine)
SessionLocal = scoped_session(_session_factory)
Base = declarative_base()


def create_isolated_session():
    """Return a brand-new Session independent of the thread-scoped registry.

    ``SessionLocal()`` returns the shared thread-local session, so helpers
    that call ``.close()`` on it tear down the caller's session as well.
    Long-running owners (patrol loop) and self-contained helpers must use
    an isolated session so closing it never affects other code paths.
    """
    return _session_factory()


# Legacy compatibility patchset for databases created before the Alembic chain
# was complete. New additive schema changes must be added through Alembic only;
# keep this tuple as a rescue path for old SQLite/PostgreSQL installs.
ADDITIVE_STARTUP_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("products", "pricing_rule_id", "ALTER TABLE products ADD COLUMN pricing_rule_id INTEGER"),
    ("products", "selling_price", "ALTER TABLE products ADD COLUMN selling_price INTEGER"),
    ("products", "custom_title_en", "ALTER TABLE products ADD COLUMN custom_title_en VARCHAR"),
    ("products", "custom_description_en", "ALTER TABLE products ADD COLUMN custom_description_en TEXT"),
    ("products", "custom_title_en_source_hash", "ALTER TABLE products ADD COLUMN custom_title_en_source_hash VARCHAR(64)"),
    ("products", "custom_description_en_source_hash", "ALTER TABLE products ADD COLUMN custom_description_en_source_hash VARCHAR(64)"),
    ("products", "custom_title_en_manually_edited", "ALTER TABLE products ADD COLUMN custom_title_en_manually_edited BOOLEAN DEFAULT FALSE NOT NULL"),
    ("products", "custom_description_en_manually_edited", "ALTER TABLE products ADD COLUMN custom_description_en_manually_edited BOOLEAN DEFAULT FALSE NOT NULL"),
    ("products", "archived", "ALTER TABLE products ADD COLUMN archived BOOLEAN DEFAULT FALSE"),
    ("products", "deleted_at", "ALTER TABLE products ADD COLUMN deleted_at TIMESTAMP"),
    ("price_lists", "layout", "ALTER TABLE price_lists ADD COLUMN layout VARCHAR DEFAULT 'grid'"),
    ("price_lists", "theme", "ALTER TABLE price_lists ADD COLUMN theme VARCHAR DEFAULT 'dark'"),
    ("price_lists", "shop_id", "ALTER TABLE price_lists ADD COLUMN shop_id INTEGER"),
    ("price_lists", "unpublish_at", "ALTER TABLE price_lists ADD COLUMN unpublish_at TIMESTAMP"),
    ("price_lists", "list_type", "ALTER TABLE price_lists ADD COLUMN list_type VARCHAR DEFAULT 'permanent' NOT NULL"),
    ("shops", "logo_url", "ALTER TABLE shops ADD COLUMN logo_url VARCHAR"),
    ("shops", "instagram_username", "ALTER TABLE shops ADD COLUMN instagram_username VARCHAR(64)"),
    ("price_lists", "shipping_note", "ALTER TABLE price_lists ADD COLUMN shipping_note VARCHAR(200)"),
    ("description_templates", "user_id", "ALTER TABLE description_templates ADD COLUMN user_id INTEGER"),
    ("products", "patrol_fail_count", "ALTER TABLE products ADD COLUMN patrol_fail_count INTEGER DEFAULT 0"),
    ("products", "last_patrolled_at", "ALTER TABLE products ADD COLUMN last_patrolled_at TIMESTAMP"),
    ("products", "next_patrol_at", "ALTER TABLE products ADD COLUMN next_patrol_at TIMESTAMP"),
    ("products", "manual_margin_rate", "ALTER TABLE products ADD COLUMN manual_margin_rate INTEGER"),
    ("products", "manual_shipping_cost", "ALTER TABLE products ADD COLUMN manual_shipping_cost INTEGER"),
    ("variants", "selling_price", "ALTER TABLE variants ADD COLUMN selling_price INTEGER"),
    ("scrape_jobs", "logical_job_id", "ALTER TABLE scrape_jobs ADD COLUMN logical_job_id VARCHAR(64)"),
    ("scrape_jobs", "parent_job_id", "ALTER TABLE scrape_jobs ADD COLUMN parent_job_id VARCHAR(64)"),
    ("scrape_jobs", "context_payload", "ALTER TABLE scrape_jobs ADD COLUMN context_payload TEXT"),
    ("scrape_jobs", "progress_current", "ALTER TABLE scrape_jobs ADD COLUMN progress_current INTEGER"),
    ("scrape_jobs", "progress_total", "ALTER TABLE scrape_jobs ADD COLUMN progress_total INTEGER"),
    ("scrape_jobs", "result_payload", "ALTER TABLE scrape_jobs ADD COLUMN result_payload TEXT"),
    ("scrape_jobs", "error_payload", "ALTER TABLE scrape_jobs ADD COLUMN error_payload TEXT"),
    ("scrape_jobs", "tracker_dismissed_at", "ALTER TABLE scrape_jobs ADD COLUMN tracker_dismissed_at TIMESTAMP"),
    ("scrape_job_events", "payload", "ALTER TABLE scrape_job_events ADD COLUMN payload TEXT"),
    ("scrape_job_events", "created_at", "ALTER TABLE scrape_job_events ADD COLUMN created_at TIMESTAMP"),
    ("products", "is_listed", "ALTER TABLE products ADD COLUMN is_listed BOOLEAN DEFAULT TRUE"),
    ("translation_suggestions", "auto_apply", "ALTER TABLE translation_suggestions ADD COLUMN auto_apply BOOLEAN DEFAULT FALSE"),
    ("translation_suggestions", "worker_token", "ALTER TABLE translation_suggestions ADD COLUMN worker_token VARCHAR(64)"),
    ("translation_suggestions", "lease_expires_at", "ALTER TABLE translation_suggestions ADD COLUMN lease_expires_at TIMESTAMP"),
    ("users", "default_pricing_rule_id", "ALTER TABLE users ADD COLUMN default_pricing_rule_id INTEGER"),
    ("users", "exchange_rate_margin", "ALTER TABLE users ADD COLUMN exchange_rate_margin INTEGER DEFAULT 0 NOT NULL"),
    ("users", "role", "ALTER TABLE users ADD COLUMN role VARCHAR(16) DEFAULT 'student' NOT NULL"),
    ("users", "last_login_at", "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP"),
)


def init_db(bind=None):
    Base.metadata.create_all(bind or engine)


def _collect_table_columns(connection, existing_tables: set[str] | None = None) -> tuple[set[str], dict[str, set[str]]]:
    inspector = inspect(connection)
    table_names = set(existing_tables or inspector.get_table_names())
    table_columns = {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in table_names
    }
    return table_names, table_columns


def _apply_additive_statement(connection, sql: str, *, owns_connection: bool) -> None:
    supports_savepoints = bool(getattr(connection.dialect, "supports_savepoints", False))
    if supports_savepoints:
        with connection.begin_nested():
            connection.execute(text(sql))
        return

    connection.execute(text(sql))
    if owns_connection and connection.in_transaction():
        connection.commit()


def apply_additive_startup_migrations(bind=None) -> dict[str, list[str]]:
    connection = bind or engine.connect()
    owns_connection = bind is None
    applied: list[str] = []
    errors: list[str] = []

    try:
        existing_tables, table_columns = _collect_table_columns(connection)
        for table, column, sql in ADDITIVE_STARTUP_MIGRATIONS:
            if table not in existing_tables:
                continue

            if column in table_columns.get(table, set()):
                continue

            try:
                _apply_additive_statement(connection, sql, owns_connection=owns_connection)
                applied.append(f"{table}.{column}")
                table_columns.setdefault(table, set()).add(column)
            except Exception as exc:
                if owns_connection and connection.in_transaction():
                    connection.rollback()
                errors.append(f"{table}.{column}: {exc}")

        if owns_connection and connection.in_transaction():
            connection.commit()

        return {
            "applied": applied,
            "errors": errors,
        }
    finally:
        if owns_connection:
            connection.close()


def inspect_additive_schema_drift(bind=None) -> dict[str, object]:
    connection = bind or engine.connect()
    owns_connection = bind is None

    try:
        existing_tables, table_columns = _collect_table_columns(connection)

        tables_with_additive_expectations = sorted({table for table, _, _ in ADDITIVE_STARTUP_MIGRATIONS})
        missing_tables = [table for table in tables_with_additive_expectations if table not in existing_tables]

        missing_columns: list[str] = []
        for table, column, _sql in ADDITIVE_STARTUP_MIGRATIONS:
            if table not in existing_tables:
                continue
            if column not in table_columns.get(table, set()):
                missing_columns.append(f"{table}.{column}")

        return {
            "database_backend": get_database_backend(),
            "database_url": redact_database_url(),
            "ready": not missing_tables and not missing_columns,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "expected_tables": tables_with_additive_expectations,
            "table_count": len(existing_tables),
            "blockers": list(missing_tables) + missing_columns,
        }
    finally:
        if owns_connection:
            connection.close()


def ensure_additive_schema_ready(bind=None) -> dict[str, object]:
    snapshot = inspect_additive_schema_drift(bind=bind)
    blockers = list(snapshot.get("blockers") or [])
    if blockers:
        raise RuntimeError(
            "Database schema drift remains after bootstrap: " + ", ".join(str(blocker) for blocker in blockers)
        )
    return snapshot


def alembic_available() -> bool:
    return bool(importlib.util.find_spec("alembic"))


def get_database_backend(database_url: str | None = None) -> str:
    resolved_url = normalize_database_url(database_url or get_database_url())
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
    resolved_url = normalize_database_url(database_url or get_database_url())
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


@contextmanager
def alembic_upgrade_lock(database_url: str):
    """Serialize Alembic upgrades on PostgreSQL.

    PostgreSQL advisory locks are session scoped, so the connection must stay
    open for the entire Alembic command. Other backends intentionally remain a
    no-op to preserve SQLite/local bootstrap behavior.
    """
    normalized_url = normalize_database_url(database_url)
    if get_database_backend(normalized_url) != "postgresql":
        yield
        return

    lock_engine = create_engine(
        normalized_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    lock_connection = None
    acquired = False
    try:
        lock_connection = lock_engine.connect()
        lock_connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": ALEMBIC_UPGRADE_ADVISORY_LOCK_ID},
        )
        acquired = True
        logger.info("Acquired PostgreSQL Alembic advisory lock")
        yield
    finally:
        if acquired and lock_connection is not None:
            try:
                lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": ALEMBIC_UPGRADE_ADVISORY_LOCK_ID},
                )
                logger.info("Released PostgreSQL Alembic advisory lock")
            except Exception as exc:
                logger.error(
                    "Failed to release PostgreSQL Alembic advisory lock: error=%s",
                    type(exc).__name__,
                )
        if lock_connection is not None:
            lock_connection.close()
        lock_engine.dispose()


def _run_alembic_upgrade(
    database_url: str,
    *,
    revision: str,
    config_path: str,
) -> str:
    from alembic import command
    from alembic.config import Config

    normalized_url = normalize_database_url(database_url)
    config = Config(config_path)
    config.attributes["configured_sqlalchemy_url"] = normalized_url
    with alembic_upgrade_lock(normalized_url):
        command.upgrade(config, revision)
    return revision


def run_alembic_upgrade(revision: str = "head", config_path: str = "alembic.ini") -> str:
    if not alembic_available():
        raise RuntimeError("Alembic is not installed")

    return _run_alembic_upgrade(
        get_database_url(),
        revision=revision,
        config_path=config_path,
    )


def run_alembic_upgrade_for_database_url(
    database_url: str,
    revision: str = "head",
    config_path: str = "alembic.ini",
) -> str:
    if not alembic_available():
        raise RuntimeError("Alembic is not installed")

    return _run_alembic_upgrade(
        database_url,
        revision=revision,
        config_path=config_path,
    )


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
