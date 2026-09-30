import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from database import Base, run_alembic_upgrade_for_database_url


def test_thumbnail_migration_preserves_legacy_products_images_and_creates_no_demand(tmp_path):
    url = f"sqlite:///{tmp_path / 'thumbs.db'}"
    run_alembic_upgrade_for_database_url(url, revision="20260930_0024")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            # The metadata baseline creates current tables; rebuild actual
            # pre-0025 shape before checking the additive upgrade.
            connection.execute(text("DROP TABLE IF EXISTS product_thumbnail_jobs"))
            connection.execute(text("INSERT INTO users (id,username,password_hash) VALUES (1,'kept','hash')"))
            connection.execute(text("INSERT INTO products (id,user_id,site,source_url,last_price,last_status,custom_title_en_manually_edited,custom_description_en_manually_edited) VALUES (1,1,'recordcity','https://www.recordcity.jp/ja/catalog/1',2600,'sold',0,0)"))
            connection.execute(text("INSERT INTO product_snapshots (id,product_id,image_urls,status,description) VALUES (1,1,'/media/kept.png','sold','Keep detailed description')"))
        run_alembic_upgrade_for_database_url(url, revision="20260930_0025")
        run_alembic_upgrade_for_database_url(url, revision="20260930_0025")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT last_price,last_status,detail_fetch_state FROM products WHERE id=1")).one() == (2600, "sold", None)
            assert connection.execute(text("SELECT image_urls,description FROM product_snapshots WHERE id=1")).one() == ("/media/kept.png", "Keep detailed description")
            assert connection.execute(text("SELECT COUNT(*) FROM product_thumbnail_jobs")).scalar_one() == 0
            assert {index["name"] for index in inspect(connection).get_indexes("product_thumbnail_jobs")} == {"ix_product_thumbnail_jobs_user_id", "ix_product_thumbnail_jobs_state_retry"}
    finally:
        engine.dispose()


def test_thumbnail_migration_is_repeatable_after_create_all_and_downgrades(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20260930_0025_add_product_thumbnail_jobs.py"
    spec = importlib.util.spec_from_file_location("thumbnail_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            migration.upgrade()
            assert "product_thumbnail_jobs" in inspect(connection).get_table_names()
            migration.downgrade()
            assert "product_thumbnail_jobs" not in inspect(connection).get_table_names()
            assert "products" in inspect(connection).get_table_names()
            migration.upgrade()
            assert "product_thumbnail_jobs" in inspect(connection).get_table_names()
    finally:
        engine.dispose()
