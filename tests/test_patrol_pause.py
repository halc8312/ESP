"""Durable deterministic-error quarantine, correction and migration checks."""
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from database import Base, run_alembic_upgrade_for_database_url
from models import Product, User, Variant
from services.monitor_service import MonitorService
from services.patrol.base_patrol import PatrolResult
from time_utils import utc_now


class Patrol:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self.result


def _create_product(db_session, *, url="https://snkrdunk.com/search?keywords=test", paused=False, username="patrol-owner"):
    user = User(username=username)
    user.set_password("testing-password")
    db_session.add(user)
    db_session.flush()
    product = Product(
        user_id=user.id, site="snkrdunk", source_url=url, last_title="Retained title",
        last_price=6500, last_status="on_sale", archived=False, is_listed=True,
        patrol_fail_count=124, updated_at=utc_now() - timedelta(days=2),
        patrol_paused_reason="invalid_url" if paused else None,
        patrol_paused_source_url=url if paused else None,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(Variant(product_id=product.id, option1_value="Default Title", inventory_qty=3, price=6500))
    db_session.commit()
    return product


def test_invalid_url_pause_is_durable_retains_last_good_data_and_emits_once(client, db_session, monkeypatch):
    product = _create_product(db_session)
    patrol = Patrol(PatrolResult(price=1, status="sold"))
    observations = []
    monkeypatch.setattr(MonitorService, "_patrols", {"snkrdunk": patrol})
    monkeypatch.setattr("services.monitor_service.record_observation_safely", lambda **kwargs: observations.append(kwargs))
    first = MonitorService.check_stale_products(limit=1)
    second = MonitorService.check_stale_products(limit=1)
    db_session.expire_all()
    saved = db_session.get(Product, product.id)
    assert first["paused_count"] == 1
    assert second["selected_count"] == 0
    assert patrol.calls == []
    assert saved.patrol_fail_count == 125
    assert saved.patrol_paused_reason == "invalid_url"
    assert saved.last_price == 6500
    assert saved.last_status == "on_sale"
    assert saved.variants[0].inventory_qty == 3
    assert len(observations) == 1
    assert observations[0]["outcome"] == "failure"
    assert observations[0]["reason"] == "invalid_url"


def test_paused_invalid_rows_do_not_starve_due_valid_products(client, db_session, monkeypatch):
    bad = _create_product(db_session, paused=True)
    good = _create_product(db_session, url="https://snkrdunk.com/apparels/123", username="other-owner")
    patrol = Patrol(PatrolResult(price=7000, status="active"))
    monkeypatch.setattr(MonitorService, "_patrols", {"snkrdunk": patrol})
    summary = MonitorService.check_stale_products(limit=1)
    assert summary["selected_count"] == 1
    assert patrol.calls == [good.source_url]
    db_session.expire_all()
    assert db_session.get(Product, good.id).last_price == 7000
    assert db_session.get(Product, bad.id).last_price == 6500
    assert db_session.get(Product, bad.id).patrol_fail_count == 124


@pytest.mark.parametrize("changed_url", [False, True])
def test_corrected_url_or_newly_supported_route_resumes_and_success_clears_failures(client, db_session, monkeypatch, changed_url):
    url = "https://snkrdunk.com/apparels/123/used/987"
    product = _create_product(db_session, url=url if not changed_url else "https://snkrdunk.com/search", paused=True)
    if changed_url:
        product.source_url = url
        db_session.commit()
    patrol = Patrol(PatrolResult(price=7000, status="active"))
    monkeypatch.setattr(MonitorService, "_patrols", {"snkrdunk": patrol})
    summary = MonitorService.check_stale_products(limit=1)
    db_session.expire_all()
    saved = db_session.get(Product, product.id)
    assert summary["resumed_count"] == 1
    assert patrol.calls == [url]
    assert saved.patrol_paused_reason is None
    assert saved.patrol_paused_source_url is None
    assert saved.patrol_fail_count == 0
    assert saved.last_price == 7000


def test_corrected_url_failed_fetch_preserves_history_and_last_good_data(client, db_session, monkeypatch):
    product = _create_product(db_session, url="https://snkrdunk.com/apparels/123", paused=True)
    patrol = Patrol(PatrolResult(error="HTTP 429"))
    monkeypatch.setattr(MonitorService, "_patrols", {"snkrdunk": patrol})
    summary = MonitorService.check_stale_products(limit=1)
    db_session.expire_all()
    saved = db_session.get(Product, product.id)
    assert summary["resumed_count"] == 1
    assert saved.patrol_fail_count == 125
    assert saved.next_patrol_at > utc_now()
    assert saved.last_price == 6500
    assert saved.last_status == "on_sale"
    assert saved.variants[0].inventory_qty == 3


def test_verified_sold_without_price_retains_previous_price_and_zeros_stock(client, db_session, monkeypatch):
    product = _create_product(db_session, url="https://snkrdunk.com/apparels/123/used/987")
    patrol = Patrol(PatrolResult(price=None, status="sold"))
    monkeypatch.setattr(MonitorService, "_patrols", {"snkrdunk": patrol})
    summary = MonitorService.check_stale_products(limit=1)
    db_session.expire_all()
    saved = db_session.get(Product, product.id)
    assert summary["successful_count"] == 1
    assert saved.last_price == 6500
    assert saved.last_status == "sold"
    assert saved.variants[0].inventory_qty == 0


def test_pause_migration_upgrades_previous_head_preserving_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'patrol-pause-migration.db'}"
    run_alembic_upgrade_for_database_url(url, revision="20260906_0022")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            # Baseline migrations use current model metadata on a fresh DB;
            # remove the new columns to reproduce the real previous schema.
            connection.execute(text("ALTER TABLE products DROP COLUMN patrol_paused_reason"))
            connection.execute(text("ALTER TABLE products DROP COLUMN patrol_paused_source_url"))
            connection.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'kept', 'hash')"))
            connection.execute(text("INSERT INTO products (id, user_id, site, source_url, last_price, last_status, patrol_fail_count, custom_title_en_manually_edited, custom_description_en_manually_edited) VALUES (1, 1, 'snkrdunk', 'https://snkrdunk.com/apparels/123', 6500, 'on_sale', 124, 0, 0)"))
        run_alembic_upgrade_for_database_url(url, revision="20260929_0023")
        run_alembic_upgrade_for_database_url(url, revision="20260929_0023")
        with engine.connect() as connection:
            saved = connection.execute(text("SELECT last_price, last_status, patrol_fail_count, patrol_paused_reason, patrol_paused_source_url FROM products WHERE id=1")).one()
            assert tuple(saved) == (6500, "on_sale", 124, None, None)
    finally:
        engine.dispose()


def test_pause_migration_handles_create_all_and_repeated_upgrade(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20260929_0023_add_product_patrol_pause.py"
    spec = importlib.util.spec_from_file_location("patrol_pause_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            migration.upgrade()
            columns = {column["name"] for column in inspect(connection).get_columns("products")}
            assert {"patrol_paused_reason", "patrol_paused_source_url"}.issubset(columns)
    finally:
        engine.dispose()
