import importlib.util

from sqlalchemy import inspect, text

import database
from models import Product, ProductSnapshot, User, Variant
from services.product_service import save_scraped_items_to_db


def test_rollback_metadata_and_bootstrap_recognize_extra_schema_without_new_handlers(db_session):
    assert importlib.util.find_spec("models_mail") is None
    assert importlib.util.find_spec("services.product_thumbnail_jobs") is None
    assert importlib.util.find_spec("services.catalog_request_notifications") is None
    assert not {"product_thumbnail_jobs", "catalog_request_notifications"}.intersection(database.Base.metadata.tables)
    assert database.bootstrap_schema("alembic") == "alembic"
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260930_0026"
        assert {"product_thumbnail_jobs", "catalog_request_notifications"}.issubset(inspect(connection).get_table_names())
        assert connection.execute(text("SELECT count(*) FROM product_thumbnail_jobs")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM catalog_request_notifications")).scalar_one() == 0


def test_normal_product_save_and_update_preserve_schema26_and_create_no_new_work(db_session):
    user = User(username="schema26-owner", password_hash="hash")
    db_session.add(user)
    db_session.commit()
    item = {"url": "https://jp.mercari.com/item/m12345678901", "title": "Legacy verified item", "price": 1200,
        "status": "on_sale", "description": "Keep original verified detail", "image_urls": []}
    result = save_scraped_items_to_db([item], user.id, site="mercari", return_summary=True)
    product = db_session.get(Product, result["product_ids"][0])
    assert product.detail_fetch_state is None and product.last_price == 1200
    assert product.variants[0].inventory_qty == 1
    product.custom_title_en = "Owner's English title"
    db_session.commit()
    item.update(price=1500, status="sold")
    save_scraped_items_to_db([item], user.id, site="mercari")
    db_session.expire_all()
    assert product.last_price == 1500 and product.last_status == "sold"
    assert product.variants[0].inventory_qty == 0
    assert product.custom_title_en == "Owner's English title"
    assert product.detail_fetch_state is None
    assert db_session.query(ProductSnapshot).count() == 2
    assert db_session.query(Variant).count() == 1
    assert db_session.execute(text("SELECT count(*) FROM product_thumbnail_jobs")).scalar_one() == 0
    assert db_session.execute(text("SELECT count(*) FROM catalog_request_notifications")).scalar_one() == 0


def test_recognized_migrations_are_standalone_without_new_application_models():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for filename, expected_parent in (
        ("20260930_0025_add_product_thumbnail_jobs.py", "20260930_0024"),
        ("20260930_0026_add_catalog_request_notifications.py", "20260930_0025"),
    ):
        spec = importlib.util.spec_from_file_location("rollback_standalone", root / "alembic/versions" / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.down_revision == expected_parent


def test_web_startup_bootstraps_schema26_and_keeps_legacy_inmemory_lock_policy(db_session, monkeypatch):
    from app import _should_try_redis_scheduler_lock, create_web_app

    # The old lock-policy test starts an unrelated real patrol thread. Keep
    # its policy assertion on the migrated fixture without launching patrol.
    monkeypatch.setattr("app.start_scheduler", lambda app: False)
    created = create_web_app(config_overrides={
        "TESTING": True, "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": True,
        "SCRAPE_QUEUE_BACKEND": "inmemory", "REDIS_URL": "redis://localhost:6379/0",
    })
    assert created.extensions["esp_schema_bootstrap_mode"] == "alembic"
    assert _should_try_redis_scheduler_lock(created) is False
    assert db_session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260930_0026"
