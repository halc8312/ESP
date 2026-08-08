import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

import database
from models import ScrapeJob, ScrapeJobEvent
from time_utils import utc_now


# Every "upgrade runs all the way to head" assertion below shares this, so a new
# migration only needs the revision updated in one place.
ALEMBIC_HEAD_REVISION = "20260808_0018"


def _coerce_datetime(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


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

    assert database.get_database_url() == "postgresql+psycopg://user:secret@example.com/app"
    assert database.get_database_backend() == "postgresql"
    assert "***" in database.redact_database_url()


class _FakeAdvisoryLockConnection:
    def __init__(self, events):
        self.events = events

    def execute(self, statement, parameters):
        self.events.append((str(statement), dict(parameters)))

    def close(self):
        self.events.append(("connection.close", {}))


class _FakeAdvisoryLockEngine:
    def __init__(self, events):
        self.events = events
        self.connection = _FakeAdvisoryLockConnection(events)

    def connect(self):
        self.events.append(("engine.connect", {}))
        return self.connection

    def dispose(self):
        self.events.append(("engine.dispose", {}))


def test_postgres_alembic_upgrade_is_wrapped_by_advisory_lock(monkeypatch):
    events = []
    monkeypatch.setattr(
        database,
        "create_engine",
        lambda *_args, **_kwargs: _FakeAdvisoryLockEngine(events),
    )
    monkeypatch.setattr(
        "alembic.command.upgrade",
        lambda _config, revision: events.append(("upgrade", {"revision": revision})),
    )

    applied = database.run_alembic_upgrade_for_database_url(
        "postgresql://user:secret@example.test/app"
    )

    assert applied == "head"
    assert events == [
        ("engine.connect", {}),
        (
            "SELECT pg_advisory_lock(:lock_id)",
            {"lock_id": database.ALEMBIC_UPGRADE_ADVISORY_LOCK_ID},
        ),
        ("upgrade", {"revision": "head"}),
        (
            "SELECT pg_advisory_unlock(:lock_id)",
            {"lock_id": database.ALEMBIC_UPGRADE_ADVISORY_LOCK_ID},
        ),
        ("connection.close", {}),
        ("engine.dispose", {}),
    ]


def test_postgres_alembic_upgrade_releases_advisory_lock_on_failure(monkeypatch):
    events = []
    monkeypatch.setattr(
        database,
        "create_engine",
        lambda *_args, **_kwargs: _FakeAdvisoryLockEngine(events),
    )

    def _fail_upgrade(_config, _revision):
        events.append(("upgrade.failed", {}))
        raise RuntimeError("migration failed")

    monkeypatch.setattr("alembic.command.upgrade", _fail_upgrade)

    with pytest.raises(RuntimeError, match="migration failed"):
        database.run_alembic_upgrade_for_database_url(
            "postgresql://user:secret@example.test/app"
        )

    assert events == [
        ("engine.connect", {}),
        (
            "SELECT pg_advisory_lock(:lock_id)",
            {"lock_id": database.ALEMBIC_UPGRADE_ADVISORY_LOCK_ID},
        ),
        ("upgrade.failed", {}),
        (
            "SELECT pg_advisory_unlock(:lock_id)",
            {"lock_id": database.ALEMBIC_UPGRADE_ADVISORY_LOCK_ID},
        ),
        ("connection.close", {}),
        ("engine.dispose", {}),
    ]


def test_sqlite_alembic_upgrade_keeps_existing_no_lock_behavior(monkeypatch):
    events = []
    monkeypatch.setattr(
        database,
        "create_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SQLite must not create a lock engine")
        ),
    )
    monkeypatch.setattr(
        "alembic.command.upgrade",
        lambda _config, revision: events.append(("upgrade", revision)),
    )

    applied = database.run_alembic_upgrade_for_database_url("sqlite:///test.sqlite")

    assert applied == "head"
    assert events == [("upgrade", "head")]


def test_create_app_engine_normalizes_legacy_postgres_scheme(monkeypatch):
    captured = {}

    def fake_create_engine(url, echo=False, **kwargs):
        captured["url"] = url

        class FakeEngine:
            url = type("FakeUrl", (), {"drivername": "postgresql"})()

        return FakeEngine()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)

    engine = database.create_app_engine("postgres://user:secret@example.com/app")

    assert captured["url"] == "postgresql+psycopg://user:secret@example.com/app"
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


def test_apply_additive_startup_migrations_adds_description_template_user_id():
    smoke_db = Path(f"test_db_additive_description_templates_{uuid.uuid4().hex}.sqlite")
    smoke_engine = database.create_app_engine(f"sqlite:///{smoke_db.as_posix()}")

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE description_templates (
                        id INTEGER PRIMARY KEY,
                        name VARCHAR NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    )
                    """
                )
            )

        with smoke_engine.begin() as connection:
            results = database.apply_additive_startup_migrations(bind=connection)
        columns = {column["name"] for column in inspect(smoke_engine).get_columns("description_templates")}
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "description_templates.user_id" in results["applied"]
    assert "user_id" in columns


def test_apply_additive_startup_migrations_adds_pricelist_unpublish_at():
    smoke_db = Path(f"test_db_additive_pricelist_schedule_{uuid.uuid4().hex}.sqlite")
    smoke_engine = database.create_app_engine(f"sqlite:///{smoke_db.as_posix()}")

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE price_lists (
                        id INTEGER PRIMARY KEY,
                        name VARCHAR NOT NULL,
                        token VARCHAR NOT NULL
                    )
                    """
                )
            )

        with smoke_engine.begin() as connection:
            results = database.apply_additive_startup_migrations(bind=connection)
        columns = {
            column["name"]
            for column in inspect(smoke_engine).get_columns("price_lists")
        }
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "price_lists.unpublish_at" in results["applied"]
    assert "unpublish_at" in columns


def test_apply_additive_startup_migrations_adds_variant_selling_price():
    smoke_db = Path(f"test_db_additive_variant_sale_{uuid.uuid4().hex}.sqlite")
    smoke_engine = database.create_app_engine(f"sqlite:///{smoke_db.as_posix()}")

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE variants (
                        id INTEGER PRIMARY KEY,
                        product_id INTEGER NOT NULL,
                        price INTEGER
                    )
                    """
                )
            )

        with smoke_engine.begin() as connection:
            results = database.apply_additive_startup_migrations(bind=connection)
        columns = {
            column["name"]
            for column in inspect(smoke_engine).get_columns("variants")
        }
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "variants.selling_price" in results["applied"]
    assert "selling_price" in columns


def test_apply_additive_startup_migrations_adds_translation_worker_lease():
    smoke_db = Path(f"test_db_additive_translation_lease_{uuid.uuid4().hex}.sqlite")
    smoke_engine = database.create_app_engine(f"sqlite:///{smoke_db.as_posix()}")

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE translation_suggestions (
                        id INTEGER PRIMARY KEY,
                        job_id VARCHAR(64) NOT NULL,
                        status VARCHAR(16) NOT NULL
                    )
                    """
                )
            )

        with smoke_engine.begin() as connection:
            results = database.apply_additive_startup_migrations(bind=connection)
        columns = {
            column["name"]
            for column in inspect(smoke_engine).get_columns(
                "translation_suggestions"
            )
        }
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "translation_suggestions.worker_token" in results["applied"]
    assert "translation_suggestions.lease_expires_at" in results["applied"]
    assert {"worker_token", "lease_expires_at"}.issubset(columns)


def test_apply_additive_startup_migrations_adds_product_patrol_schedule_columns():
    smoke_db = Path(f"test_db_additive_product_patrol_{uuid.uuid4().hex}.sqlite")
    smoke_engine = database.create_app_engine(f"sqlite:///{smoke_db.as_posix()}")

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE products (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        site VARCHAR NOT NULL,
                        source_url VARCHAR NOT NULL,
                        patrol_fail_count INTEGER DEFAULT 0,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    )
                    """
                )
            )

        with smoke_engine.begin() as connection:
            results = database.apply_additive_startup_migrations(bind=connection)
        columns = {column["name"] for column in inspect(smoke_engine).get_columns("products")}
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "products.last_patrolled_at" in results["applied"]
    assert "products.next_patrol_at" in results["applied"]
    assert "last_patrolled_at" in columns
    assert "next_patrol_at" in columns


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
            table_names = set(inspect(upgraded_engine).get_table_names())
            with upgraded_engine.connect() as connection:
                version_num = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "tracker_dismissed_at" in columns
    assert "ix_scrape_jobs_tracker_dismissed_at" in indexes
    assert "selector_repair_candidates" in table_names
    assert "selector_active_rule_sets" in table_names
    assert version_num == ALEMBIC_HEAD_REVISION


def test_run_alembic_upgrade_adds_product_patrol_schedule_columns_from_previous_head():
    smoke_db = Path(f"test_db_alembic_patrol_schedule_{uuid.uuid4().hex}.sqlite")
    smoke_db_url = f"sqlite:///{smoke_db.resolve().as_posix()}"
    smoke_engine = database.create_app_engine(smoke_db_url)

    try:
        with smoke_engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260422_0006')"))
            connection.execute(
                text(
                    """
                    CREATE TABLE products (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        site VARCHAR NOT NULL,
                        source_url VARCHAR NOT NULL,
                        patrol_fail_count INTEGER DEFAULT 0,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    )
                    """
                )
            )
            legacy_backoff_until = utc_now() + timedelta(hours=1)
            connection.execute(
                text(
                    """
                    INSERT INTO products (
                        id,
                        user_id,
                        site,
                        source_url,
                        patrol_fail_count,
                        created_at,
                        updated_at
                    ) VALUES (
                        1,
                        1,
                        'mercari',
                        'https://jp.mercari.com/item/m123',
                        2,
                        :created_at,
                        :updated_at
                    )
                    """
                ),
                {
                    "created_at": utc_now() - timedelta(days=2),
                    "updated_at": legacy_backoff_until,
                },
            )

        database.run_alembic_upgrade_for_database_url(smoke_db_url)

        upgraded_engine = database.create_app_engine(smoke_db_url)
        try:
            inspector = inspect(upgraded_engine)
            product_columns = {column["name"] for column in inspector.get_columns("products")}
            product_indexes = {index["name"] for index in inspector.get_indexes("products")}
            with upgraded_engine.connect() as connection:
                version_num = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                migrated_product = connection.execute(
                    text(
                        """
                        SELECT updated_at, last_patrolled_at, next_patrol_at
                        FROM products
                        WHERE id = 1
                        """
                    )
                ).mappings().one()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "last_patrolled_at" in product_columns
    assert "next_patrol_at" in product_columns
    assert "ix_products_last_patrolled_at" in product_indexes
    assert "ix_products_next_patrol_at" in product_indexes
    assert version_num == ALEMBIC_HEAD_REVISION
    assert _coerce_datetime(migrated_product["next_patrol_at"]) == legacy_backoff_until
    assert _coerce_datetime(migrated_product["updated_at"]) <= utc_now()
    assert _coerce_datetime(migrated_product["last_patrolled_at"]) <= utc_now()


def test_run_alembic_upgrade_adds_pricelist_unpublish_at_from_previous_head():
    smoke_db = Path(f"test_db_alembic_pricelist_schedule_{uuid.uuid4().hex}.sqlite")
    smoke_db_url = f"sqlite:///{smoke_db.resolve().as_posix()}"
    smoke_engine = database.create_app_engine(smoke_db_url)

    try:
        with smoke_engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260610_0010')"))
            connection.execute(
                text(
                    """
                    CREATE TABLE price_lists (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        name VARCHAR NOT NULL,
                        token VARCHAR NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMP,
                        updated_at TIMESTAMP
                    )
                    """
                )
            )

        database.run_alembic_upgrade_for_database_url(smoke_db_url)

        upgraded_engine = database.create_app_engine(smoke_db_url)
        try:
            columns = {
                column["name"]
                for column in inspect(upgraded_engine).get_columns("price_lists")
            }
            with upgraded_engine.connect() as connection:
                version_num = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "unpublish_at" in columns
    assert version_num == ALEMBIC_HEAD_REVISION


def test_run_alembic_upgrade_adds_translation_manual_flags():
    smoke_db = Path(f"test_db_alembic_translation_flags_{uuid.uuid4().hex}.sqlite")
    smoke_db_url = f"sqlite:///{smoke_db.resolve().as_posix()}"
    smoke_engine = database.create_app_engine(smoke_db_url)

    try:
        with smoke_engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260729_0011')"))
            connection.execute(
                text(
                    """
                    CREATE TABLE products (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        site VARCHAR NOT NULL,
                        source_url VARCHAR NOT NULL,
                        custom_title_en VARCHAR,
                        custom_description_en TEXT,
                        custom_title_en_source_hash VARCHAR(64),
                        custom_description_en_source_hash VARCHAR(64)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO products (
                        id,
                        user_id,
                        site,
                        source_url,
                        custom_title_en,
                        custom_description_en,
                        custom_title_en_source_hash,
                        custom_description_en_source_hash
                    )
                    VALUES
                        (
                            1,
                            1,
                            'manual',
                            'https://example.com/manual-flags',
                            NULL,
                            NULL,
                            NULL,
                            NULL
                        ),
                        (
                            2,
                            1,
                            'manual',
                            'https://example.com/cleared-flags',
                            '',
                            NULL,
                            'old-title-hash',
                            'old-description-hash'
                        ),
                        (
                            3,
                            1,
                            'manual',
                            'https://example.com/populated-flags',
                            'Existing automatic title',
                            '<p>Existing automatic description</p>',
                            'title-hash',
                            'description-hash'
                        )
                    """
                )
            )

        database.run_alembic_upgrade_for_database_url(smoke_db_url)

        upgraded_engine = database.create_app_engine(smoke_db_url)
        try:
            product_columns = {
                column["name"]
                for column in inspect(upgraded_engine).get_columns("products")
            }
            with upgraded_engine.connect() as connection:
                version_num = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                stored = connection.execute(
                    text(
                        """
                        SELECT
                            id,
                            custom_title_en_manually_edited,
                            custom_description_en_manually_edited
                        FROM products
                        ORDER BY id
                        """
                    )
                ).all()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "custom_title_en_manually_edited" in product_columns
    assert "custom_description_en_manually_edited" in product_columns
    assert [tuple(row) for row in stored] == [
        (1, 0, 0),
        (2, 1, 1),
        (3, 0, 0),
    ]
    assert version_num == ALEMBIC_HEAD_REVISION


def test_run_alembic_upgrade_adds_variant_selling_price():
    smoke_db = Path(f"test_db_alembic_variant_sale_{uuid.uuid4().hex}.sqlite")
    smoke_db_url = f"sqlite:///{smoke_db.resolve().as_posix()}"
    smoke_engine = database.create_app_engine(smoke_db_url)

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            connection.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('20260729_0012')"
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE variants (
                        id INTEGER PRIMARY KEY,
                        product_id INTEGER NOT NULL,
                        price INTEGER
                    )
                    """
                )
            )

        database.run_alembic_upgrade_for_database_url(smoke_db_url)

        upgraded_engine = database.create_app_engine(smoke_db_url)
        try:
            variant_columns = {
                column["name"]
                for column in inspect(upgraded_engine).get_columns("variants")
            }
            with upgraded_engine.connect() as connection:
                version_num = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert "selling_price" in variant_columns
    assert version_num == ALEMBIC_HEAD_REVISION


def test_run_alembic_upgrade_adds_translation_worker_lease_and_recovers_legacy_running(
    monkeypatch,
):
    smoke_db = Path(f"test_db_alembic_translation_lease_{uuid.uuid4().hex}.sqlite")
    smoke_db_url = f"sqlite:///{smoke_db.resolve().as_posix()}"
    smoke_engine = database.create_app_engine(smoke_db_url)
    updated_at = datetime(2026, 7, 29, 1, 2, 3)
    app_logger = logging.getLogger("app_runtime")
    worker_logger = logging.getLogger("worker_runtime")
    monkeypatch.setattr(app_logger, "disabled", False)
    monkeypatch.setattr(worker_logger, "disabled", False)

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('20260729_0013')"
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE translation_suggestions (
                        id INTEGER PRIMARY KEY,
                        job_id VARCHAR(64) NOT NULL,
                        status VARCHAR(16) NOT NULL,
                        updated_at TIMESTAMP NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO translation_suggestions (
                        id,
                        job_id,
                        status,
                        updated_at
                    )
                    VALUES (1, 'legacy-running', 'running', :updated_at)
                    """
                ),
                {"updated_at": updated_at},
            )

        database.run_alembic_upgrade_for_database_url(smoke_db_url)

        upgraded_engine = database.create_app_engine(smoke_db_url)
        try:
            columns = {
                column["name"]
                for column in inspect(upgraded_engine).get_columns(
                    "translation_suggestions"
                )
            }
            indexes = {
                index["name"]
                for index in inspect(upgraded_engine).get_indexes(
                    "translation_suggestions"
                )
            }
            with upgraded_engine.connect() as connection:
                version_num = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                lease_expires_at = connection.execute(
                    text(
                        """
                        SELECT lease_expires_at
                        FROM translation_suggestions
                        WHERE id = 1
                        """
                    )
                ).scalar_one()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert {"worker_token", "lease_expires_at"}.issubset(columns)
    assert "ix_translation_suggestions_lease_expires_at" in indexes
    assert _coerce_datetime(lease_expires_at) == updated_at
    assert version_num == ALEMBIC_HEAD_REVISION
    assert app_logger.disabled is False
    assert worker_logger.disabled is False


def test_translation_worker_lease_migration_preserves_active_additive_claim():
    smoke_db = Path(
        f"test_db_alembic_translation_active_lease_{uuid.uuid4().hex}.sqlite"
    )
    smoke_db_url = f"sqlite:///{smoke_db.resolve().as_posix()}"
    smoke_engine = database.create_app_engine(smoke_db_url)
    legacy_updated_at = datetime(2026, 7, 29, 1, 2, 3)
    active_updated_at = datetime(2026, 7, 29, 1, 5, 0)
    active_lease = active_updated_at + timedelta(minutes=30)

    try:
        with smoke_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('20260729_0013')"
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE translation_suggestions (
                        id INTEGER PRIMARY KEY,
                        job_id VARCHAR(64) NOT NULL,
                        status VARCHAR(16) NOT NULL,
                        updated_at TIMESTAMP NOT NULL,
                        worker_token VARCHAR(64),
                        lease_expires_at TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO translation_suggestions (
                        id,
                        job_id,
                        status,
                        updated_at,
                        worker_token,
                        lease_expires_at
                    )
                    VALUES
                        (
                            1,
                            'legacy-running',
                            'running',
                            :legacy_updated_at,
                            NULL,
                            NULL
                        ),
                        (
                            2,
                            'active-running',
                            'running',
                            :active_updated_at,
                            'active-worker',
                            :active_lease
                        ),
                        (
                            3,
                            'token-only-running',
                            'running',
                            :active_updated_at,
                            'active-worker-without-lease',
                            NULL
                        )
                    """
                ),
                {
                    "legacy_updated_at": legacy_updated_at,
                    "active_updated_at": active_updated_at,
                    "active_lease": active_lease,
                },
            )

        database.run_alembic_upgrade_for_database_url(smoke_db_url)

        upgraded_engine = database.create_app_engine(smoke_db_url)
        try:
            with upgraded_engine.connect() as connection:
                stored = connection.execute(
                    text(
                        """
                        SELECT
                            id,
                            worker_token,
                            lease_expires_at
                        FROM translation_suggestions
                        ORDER BY id
                        """
                    )
                ).all()
        finally:
            upgraded_engine.dispose()
    finally:
        smoke_engine.dispose()
        smoke_db.unlink(missing_ok=True)

    assert _coerce_datetime(stored[0].lease_expires_at) == legacy_updated_at
    assert stored[0].worker_token is None
    assert stored[1].worker_token == "active-worker"
    assert _coerce_datetime(stored[1].lease_expires_at) == active_lease
    assert stored[2].worker_token == "active-worker-without-lease"
    assert stored[2].lease_expires_at is None


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
