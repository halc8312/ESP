from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import re
from threading import Barrier
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, create_isolated_session, run_alembic_upgrade_for_database_url
from models import CatalogRequest, CatalogRequestItem, PriceList, PriceListItem, Product, Shop, User, Variant
from services import catalog_request_service as service
from time_utils import utc_now


@pytest.fixture
def catalog(db_session):
    owner = User(username="inquiry_owner")
    owner.set_password("testpassword")
    other = User(username="inquiry_other")
    other.set_password("testpassword")
    db_session.add_all([owner, other])
    db_session.flush()
    shop = Shop(user_id=owner.id, name="Inquiry Shop")
    db_session.add(shop)
    db_session.flush()
    pricelist = PriceList(user_id=owner.id, shop_id=shop.id, name="Autumn Catalog", token=uuid.uuid4().hex)
    db_session.add(pricelist)
    db_session.flush()
    products, rows = [], []
    for number in range(2):
        product = Product(
            user_id=owner.id, shop_id=shop.id, site="manual",
            source_url=f"https://supplier.example.invalid/private-{number}",
            custom_title=f"Curated Product {number}", last_title=f"Raw Product {number}",
            selling_price=1200 + number * 100, last_price=300, last_status="on_sale",
            is_listed=False,
            variants=[Variant(inventory_qty=3, price=300)],
        )
        db_session.add(product)
        db_session.flush()
        row = PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True, sort_order=number)
        db_session.add(row)
        products.append(product)
        rows.append(row)
    db_session.commit()
    return SimpleNamespace(owner=owner, other=other, shop=shop, pricelist=pricelist, products=products, rows=rows)


def payload_for(catalog, *, include_second=False, **overrides):
    selected = catalog.products if include_second else catalog.products[:1]
    return {
        "submission_key": uuid.uuid4().hex,
        "buyer_instagram": "https://www.instagram.com/Example.Buyer/",
        "buyer_name": "Buyer Name", "message": "Please confirm shipping and size.",
        "items": [{"product_id": product.id, "quantity": 1, "expected_price_jpy": product.selling_price} for product in selected],
        **overrides,
    }


def submit(client, catalog, payload=None):
    return client.post(f"/catalog/{catalog.pricelist.token}/requests", json=payload or payload_for(catalog))


def login_as(client, user):
    return client.post("/login", data={"username": user.username, "password": "testpassword"})


def test_submit_creates_complete_public_snapshots_without_reserving_stock(client, db_session, catalog):
    catalog.rows[0].custom_price = 1999
    db_session.commit()
    payload = payload_for(catalog, include_second=True)
    payload["items"][0].update(expected_price_jpy=1999, quantity=2)
    response = submit(client, catalog, payload)
    assert response.status_code == 201
    assert set(response.json) == {"ok", "reference"}
    assert response.json["ok"] is True
    assert len(response.json["reference"]) == 20
    saved = db_session.query(CatalogRequest).one()
    assert saved.reference == response.json["reference"]
    assert (saved.user_id, saved.pricelist_id, saved.shop_id) == (catalog.owner.id, catalog.pricelist.id, catalog.shop.id)
    assert (saved.pricelist_name, saved.shop_name) == ("Autumn Catalog", "Inquiry Shop")
    assert saved.buyer_instagram == "example.buyer"
    assert saved.viewed_at is None
    assert [(item.title_snapshot, item.price_jpy_snapshot, item.quantity) for item in saved.items] == [
        ("Curated Product 0", 1999, 2), ("Curated Product 1", 1300, 1),
    ]
    assert len(saved.payload_hash) == 64
    db_session.expire_all()
    assert catalog.products[0].variants[0].inventory_qty == 3
    assert "supplier" not in response.get_data(as_text=True)


def test_public_request_requires_csrf_and_returns_json_error(app, client, db_session, catalog, monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    response = submit(client, catalog)
    assert response.status_code == 400
    assert response.is_json
    assert response.json["code"] == "csrf_failed"
    assert db_session.query(CatalogRequest).count() == 0


def test_public_request_accepts_rendered_csrf_token_over_https(app, client, db_session, catalog, monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    path = f"/catalog/{catalog.pricelist.token}"
    page = client.get(path, base_url="https://localhost")
    assert page.status_code == 200
    config_match = re.search(r'<script type="application/json" id="catalogRequestConfig">(.*?)</script>', page.get_data(as_text=True), re.S)
    assert config_match is not None
    config = json.loads(config_match.group(1))
    assert config["csrf_token"]
    payload = payload_for(catalog)
    # A valid token still requires the matching HTTPS Referer.
    missing_referer = client.post(path + "/requests", base_url="https://localhost", json=payload,
                                  headers={"X-CSRFToken": config["csrf_token"]})
    assert missing_referer.status_code == 400
    assert missing_referer.json["code"] == "csrf_failed"
    response = client.post(path + "/requests", base_url="https://localhost", json=payload,
                           headers={"X-CSRFToken": config["csrf_token"], "Referer": "https://localhost" + path,
                                    "Accept": "application/json"})
    assert response.status_code == 201
    assert response.json["ok"] is True
    assert db_session.query(CatalogRequest).count() == 1


@pytest.mark.parametrize("price", [None, 0])
def test_unknown_or_zero_public_price_is_retained(client, db_session, catalog, price):
    product = catalog.products[0]
    product.selling_price = product.last_price = price
    product.variants[0].price = price
    db_session.commit()
    response = submit(client, catalog)
    assert response.status_code == 201
    assert db_session.query(CatalogRequestItem).one().price_jpy_snapshot == price


def test_identical_retry_returns_receipt_even_after_product_changes(client, db_session, catalog):
    payload = payload_for(catalog)
    first = submit(client, catalog, payload)
    catalog.products[0].selling_price = 9000
    catalog.products[0].variants[0].inventory_qty = 0
    db_session.commit()
    payload["submission_key"] = str(uuid.UUID(payload["submission_key"]))
    payload["buyer_instagram"] = "@EXAMPLE.BUYER"
    retry = submit(client, catalog, payload)
    assert retry.status_code == 200
    assert retry.json == first.json
    assert db_session.query(CatalogRequest).count() == 1
    assert db_session.query(CatalogRequestItem).count() == 1


def test_reusing_a_key_with_different_content_is_rejected(client, db_session, catalog):
    payload = payload_for(catalog)
    assert submit(client, catalog, payload).status_code == 201
    payload["message"] = "Changed request"
    response = submit(client, catalog, payload)
    assert response.status_code == 409
    assert response.json["code"] == "duplicate_submission"
    assert db_session.query(CatalogRequest).one().message == "Please confirm shipping and size."


def test_same_owner_key_cannot_be_replayed_through_another_catalog(client, db_session, catalog):
    payload = payload_for(catalog)
    assert submit(client, catalog, payload).status_code == 201
    second = PriceList(user_id=catalog.owner.id, name="Second catalog", token=uuid.uuid4().hex)
    db_session.add(second)
    db_session.commit()
    response = client.post(f"/catalog/{second.token}/requests", json=payload)
    assert response.status_code == 409
    assert response.json["code"] == "duplicate_submission"
    assert db_session.query(CatalogRequest).count() == 1


def test_concurrent_identical_submissions_create_only_one_request(app, db_session, catalog, monkeypatch):
    token = catalog.pricelist.token
    payload = payload_for(catalog, include_second=True)
    barrier = Barrier(2)
    original_consume = service._consume_attempt

    def overlap(token, ip):
        original_consume(token, ip)
        barrier.wait(timeout=10)

    monkeypatch.setattr(service, "_consume_attempt", overlap)

    def send():
        with create_isolated_session() as session_db:
            return service.submit_catalog_request(session_db, token, payload, "test-client")

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = [future.result(timeout=15) for future in [executor.submit(send), executor.submit(send)]]
    assert sorted(status for _, status in responses) == [200, 201]
    assert responses[0][0] == responses[1][0]
    assert db_session.query(CatalogRequest).count() == 1
    assert db_session.query(CatalogRequestItem).count() == 2


def test_changed_price_returns_current_public_data_and_saves_nothing(client, db_session, catalog):
    payload = payload_for(catalog, include_second=True)
    catalog.rows[1].custom_price = 1700
    db_session.commit()
    response = submit(client, catalog, payload)
    assert response.status_code == 409
    assert response.json["code"] == "catalog_changed"
    assert [item["price"] for item in response.json["items"]] == [1200, 1700]
    assert all("source_url" not in item and "site" not in item for item in response.json["items"])
    assert db_session.query(CatalogRequest).count() == 0
    assert db_session.query(CatalogRequestItem).count() == 0
    payload["items"][1]["expected_price_jpy"] = 1700
    assert submit(client, catalog, payload).status_code == 201


@pytest.mark.parametrize("state", ["hidden", "archived", "deleted", "removed", "foreign_owner", "sold_out", "insufficient"])
def test_unavailable_selection_is_all_or_nothing(client, db_session, catalog, state):
    payload = payload_for(catalog, include_second=True)
    product = catalog.products[1]
    if state == "hidden":
        catalog.rows[1].visible = False
    elif state == "archived":
        product.archived = True
    elif state == "deleted":
        product.deleted_at = utc_now()
    elif state == "removed":
        db_session.delete(catalog.rows[1])
    elif state == "foreign_owner":
        product.user_id = catalog.other.id
    elif state == "sold_out":
        product.variants[0].inventory_qty = 0
    else:
        payload["items"][1]["quantity"] = 4
    db_session.commit()
    response = submit(client, catalog, payload)
    assert response.status_code == 409
    assert response.json["code"] == "items_unavailable"
    expected_ids = [catalog.products[0].id]
    if state in ("sold_out", "insufficient"):
        expected_ids.append(product.id)
    assert [item["product_id"] for item in response.json["items"]] == expected_ids
    assert db_session.query(CatalogRequest).count() == 0
    assert db_session.query(CatalogRequestItem).count() == 0


@pytest.mark.parametrize("state", ["inactive", "expired", "suspended", "unknown_token"])
def test_unavailable_catalog_rejects_submissions(client, db_session, catalog, state):
    if state == "inactive":
        catalog.pricelist.is_active = False
    elif state == "expired":
        catalog.pricelist.unpublish_at = utc_now() - timedelta(seconds=1)
    elif state == "suspended":
        catalog.owner.suspended_at = utc_now()
    db_session.commit()
    token = "missing-catalog" if state == "unknown_token" else catalog.pricelist.token
    response = client.post(f"/catalog/{token}/requests", json=payload_for(catalog))
    assert response.status_code == 404
    assert response.json["code"] == "catalog_unavailable"
    assert db_session.query(CatalogRequest).count() == 0


@pytest.mark.parametrize("change", [
    {"buyer_instagram": ""}, {"buyer_instagram": "not a username"},
    {"buyer_instagram": "."}, {"buyer_instagram": ".."},
    {"buyer_instagram": ".buyer"}, {"buyer_instagram": "buyer."},
    {"buyer_instagram": "example..buyer"},
    {"buyer_name": "n" * 101}, {"message": "m" * 2001},
    {"message": "bad\x00text"}, {"submission_key": "invalid"}, {"items": []},
])
def test_invalid_contact_or_payload_is_rejected(client, db_session, catalog, change):
    response = submit(client, catalog, payload_for(catalog, **change))
    assert response.status_code == 400
    assert response.json["code"] == "validation_error"
    assert db_session.query(CatalogRequest).count() == 0


@pytest.mark.parametrize("quantity", [0, 100, True, "2", 1.5])
def test_invalid_quantity_is_rejected(client, db_session, catalog, quantity):
    payload = payload_for(catalog)
    payload["items"][0]["quantity"] = quantity
    assert submit(client, catalog, payload).status_code == 400
    assert db_session.query(CatalogRequest).count() == 0


def test_duplicate_or_excess_product_rows_are_rejected(client, db_session, catalog):
    payload = payload_for(catalog)
    payload["items"] *= 2
    assert submit(client, catalog, payload).status_code == 400
    payload["items"] *= 26
    assert submit(client, catalog, payload).status_code == 400
    assert db_session.query(CatalogRequestItem).count() == 0


def test_request_rate_limit_and_idempotent_receipt(client, db_session, catalog):
    first_payload = payload_for(catalog)
    first = submit(client, catalog, first_payload)
    assert first.status_code == 201
    for _ in range(4):
        assert submit(client, catalog).status_code == 201
    response = submit(client, catalog)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"
    assert response.json["code"] == "rate_limited"
    retry = submit(client, catalog, first_payload)
    assert retry.status_code == 200 and retry.json == first.json
    assert db_session.query(CatalogRequest).count() == 5


def test_unavailable_rate_store_fails_closed(client, db_session, catalog, monkeypatch):
    def broken_store():
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(service, "get_rate_limiter", broken_store)
    response = submit(client, catalog)
    assert response.status_code == 503
    assert response.json["code"] == "service_unavailable"
    assert db_session.query(CatalogRequest).count() == 0


def test_owner_inbox_detail_and_explicit_read_lifecycle(client, db_session, catalog):
    receipt = submit(client, catalog).json
    saved = db_session.query(CatalogRequest).one()
    request_id = saved.id
    assert client.get("/requests").status_code == 302
    assert client.get(f"/requests/{request_id}").status_code == 302
    assert client.post(f"/requests/{request_id}/read").status_code == 302
    login_as(client, catalog.owner)
    listing = client.get("/requests")
    assert listing.status_code == 200
    assert receipt["reference"] in listing.get_data(as_text=True)
    detail = client.get(f"/requests/{request_id}")
    assert detail.status_code == 200
    assert "example.buyer" in detail.get_data(as_text=True)
    assert "Curated Product 0" in detail.get_data(as_text=True)
    db_session.expire_all()
    assert saved.viewed_at is None
    assert client.get(f"/requests/{request_id}/read").status_code == 405
    assert client.post(f"/requests/{request_id}/read").status_code == 302
    db_session.expire_all()
    viewed_at = saved.viewed_at
    assert viewed_at is not None
    assert receipt["reference"] not in client.get("/requests").get_data(as_text=True)
    assert receipt["reference"] in client.get("/requests?filter=all").get_data(as_text=True)
    assert client.post(f"/requests/{request_id}/read").status_code == 302
    db_session.expire_all()
    assert saved.viewed_at == viewed_at


def test_other_student_cannot_view_or_mark_request(client, db_session, catalog):
    submit(client, catalog)
    saved = db_session.query(CatalogRequest).one()
    request_id = saved.id
    login_as(client, catalog.other)
    listing = client.get("/requests?filter=all")
    assert listing.status_code == 200
    assert saved.reference not in listing.get_data(as_text=True)
    assert client.get(f"/requests/{request_id}").status_code == 404
    assert client.post(f"/requests/{request_id}/read").status_code == 404
    db_session.expire_all()
    assert saved.viewed_at is None


def test_inbox_paginates_25_requests_and_filters_unread(client, db_session, catalog):
    for index in range(27):
        db_session.add(CatalogRequest(
            reference=f"reference-{index:010d}", user_id=catalog.owner.id,
            pricelist_name="Past catalog", buyer_instagram="buyer",
            submission_key=uuid.uuid4().hex, payload_hash="a" * 64,
            created_at=utc_now() + timedelta(seconds=index),
            viewed_at=utc_now() if index == 0 else None,
        ))
    db_session.commit()
    login_as(client, catalog.owner)
    page_one = client.get("/requests?filter=all").get_data(as_text=True)
    page_two = client.get("/requests?filter=all&page=2").get_data(as_text=True)
    assert "reference-0000000002" in page_one
    assert "reference-0000000001" not in page_one
    assert "reference-0000000001" in page_two
    assert "reference-0000000000" in page_two
    assert "reference-0000000002" not in page_two
    unread = client.get("/requests?filter=unread&page=2").get_data(as_text=True)
    assert "reference-0000000001" in unread
    assert "reference-0000000000" not in unread


def test_saved_snapshot_survives_product_catalog_and_shop_deletion(client, db_session, catalog):
    # Enable actual FK actions on the deletion connection, as in PostgreSQL.
    db_session.execute(text("PRAGMA foreign_keys=ON"))
    submit(client, catalog)
    saved = db_session.query(CatalogRequest).one()
    original_title = saved.items[0].title_snapshot
    db_session.delete(catalog.pricelist)
    for product in catalog.products:
        db_session.delete(product)
    db_session.flush()
    db_session.delete(catalog.shop)
    db_session.commit()
    db_session.expire_all()
    assert saved.pricelist_id is None
    assert saved.shop_id is None
    assert saved.pricelist_name == "Autumn Catalog"
    assert saved.shop_name == "Inquiry Shop"
    assert saved.items[0].product_id is None
    assert saved.items[0].title_snapshot == original_title
    assert saved.items[0].price_jpy_snapshot == 1200


def test_migration_upgrades_previous_head_and_preserves_existing_rows(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'requests-migration.db'}"
    run_alembic_upgrade_for_database_url(database_url, revision="20260905_0021")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            assert "catalog_requests" not in inspect(connection).get_table_names()
            connection.execute(text("INSERT INTO users (id, username, password_hash) VALUES (123, 'retained-owner', 'hash')"))
        run_alembic_upgrade_for_database_url(database_url, revision="20260906_0022")
        run_alembic_upgrade_for_database_url(database_url, revision="20260906_0022")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT username FROM users WHERE id=123")).scalar_one() == "retained-owner"
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260906_0022"
            inspector = inspect(connection)
            for model in (CatalogRequest, CatalogRequestItem):
                actual = {column["name"] for column in inspector.get_columns(model.__tablename__)}
                assert actual == set(model.__table__.columns.keys())
            header_fks = {fk["constrained_columns"][0]: fk.get("options", {}).get("ondelete") for fk in inspector.get_foreign_keys("catalog_requests")}
            assert header_fks["pricelist_id"] == "SET NULL"
            assert header_fks["shop_id"] == "SET NULL"
            constraints = inspector.get_unique_constraints("catalog_requests")
            assert any(set(item["column_names"]) == {"user_id", "submission_key"} for item in constraints)
        with Session(engine) as session_db:
            request = CatalogRequest(
                reference="migration-receipt", user_id=123, pricelist_name="Removed catalog",
                buyer_instagram="buyer", submission_key=uuid.uuid4().hex, payload_hash="b" * 64,
                items=[CatalogRequestItem(title_snapshot="Retained item", quantity=2, price_jpy_snapshot=None)],
            )
            session_db.add(request)
            session_db.commit()
            assert session_db.query(CatalogRequestItem).one().quantity == 2
            request.items[0].quantity = 100
            with pytest.raises(IntegrityError):
                session_db.commit()
            session_db.rollback()
    finally:
        engine.dispose()


def test_migration_handles_create_all_bootstrap_and_repeat_upgrade(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20260906_0022_add_catalog_requests.py"
    spec = importlib.util.spec_from_file_location("catalog_requests_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.downgrade()
            tables = inspect(connection).get_table_names()
            assert "catalog_requests" not in tables and "catalog_request_items" not in tables
            assert "users" in tables and "products" in tables
    finally:
        engine.dispose()
