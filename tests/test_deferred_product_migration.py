import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from database import Base, run_alembic_upgrade_for_database_url


COLUMNS = (
    "detail_fetch_state", "detail_job_id", "detail_source_url",
    "detail_scope_key",
    "detail_lease_expires_at", "detail_retry_at", "detail_fail_count",
    "detail_error_code", "detail_translate_requested",
)


def test_deferred_details_migration_preserves_legacy_values_and_null_state(tmp_path):
    url = f"sqlite:///{tmp_path / 'details.db'}"
    run_alembic_upgrade_for_database_url(url, revision="20260929_0023")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            # The first baseline migration imports current model metadata;
            # reconstruct a real pre-0024 schema before exercising the upgrade.
            connection.execute(text("DROP INDEX IF EXISTS ix_products_detail_fetch_state"))
            for column in COLUMNS:
                connection.execute(text(f"ALTER TABLE products DROP COLUMN {column}"))
            connection.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'kept', 'hash')"))
            connection.execute(text("INSERT INTO products (id, user_id, site, source_url, last_price, last_status, custom_title_en_manually_edited, custom_description_en_manually_edited) VALUES (1,1,'recordcity','https://www.recordcity.jp/ja/catalog/1',2600,'sold',0,0)"))
        run_alembic_upgrade_for_database_url(url, revision="20260930_0024")
        run_alembic_upgrade_for_database_url(url, revision="20260930_0024")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT last_price,last_status,detail_fetch_state,detail_fail_count,detail_translate_requested FROM products WHERE id=1")).one()
            assert tuple(row) == (2600, "sold", None, None, None)
            assert set(COLUMNS).issubset({column["name"] for column in inspect(connection).get_columns("products")})
    finally:
        engine.dispose()


def test_deferred_details_migration_is_repeatable_after_create_all_and_downgrades(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20260930_0024_add_deferred_product_details.py"
    spec = importlib.util.spec_from_file_location("deferred_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            migration.upgrade()
            assert set(COLUMNS).issubset({column["name"] for column in inspect(connection).get_columns("products")})
            migration.downgrade()
            assert not set(COLUMNS).intersection({column["name"] for column in inspect(connection).get_columns("products")})
            migration.upgrade()
            assert set(COLUMNS).issubset({column["name"] for column in inspect(connection).get_columns("products")})
    finally:
        engine.dispose()
