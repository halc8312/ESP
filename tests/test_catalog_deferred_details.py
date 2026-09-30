"""Public deferred details must stay scoped and cannot make unknown stock orderable."""
import json
import re
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest

from models import CatalogRequest, PriceList, PriceListItem, Product, ProductSnapshot, Shop, User, Variant
from services import product_detail_jobs
from time_utils import utc_now


@pytest.fixture
def deferred_catalog(db_session, monkeypatch):
    owner = User(username="deferred_owner")
    owner.set_password("password")
    other = User(username="deferred_other")
    other.set_password("password")
    db_session.add_all([owner, other])
    db_session.flush()
    shop = Shop(user_id=owner.id, name="Owner shop")
    other_shop = Shop(user_id=other.id, name="Foreign shop")
    same_owner_shop = Shop(user_id=owner.id, name="Another shop")
    db_session.add_all([shop, other_shop, same_owner_shop])
    db_session.flush()
    product = Product(
        user_id=owner.id, shop_id=shop.id, site="recordcity",
        source_url="https://www.recordcity.jp/ja/catalog/123456",
        last_title="Public record", last_price=777, selling_price=1200,
        last_status="unknown", detail_fetch_state="pending",
        detail_error_code="supplier_private_error", detail_source_url="https://private.example/secret",
        variants=[Variant(price=777, inventory_qty=5)],
    )
    pricelist = PriceList(user_id=owner.id, shop_id=shop.id, name="Public list", token=uuid.uuid4().hex)
    db_session.add_all([product, pricelist])
    db_session.flush()
    row = PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True)
    snapshot = ProductSnapshot(product_id=product.id, title="Public record", price=777,
                               image_urls="/media/product_images/record.jpg|https://supplier.invalid/source.jpg")
    db_session.add_all([row, snapshot])
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(product_detail_jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    return SimpleNamespace(owner=owner, other=other, shop=shop, other_shop=other_shop,
                           same_owner_shop=same_owner_shop, product=product, pricelist=pricelist,
                           row=row, dispatched=dispatched)


def endpoint(catalog):
    return f"/catalog/{catalog.pricelist.token}/products/{catalog.product.id}/details"


def inquiry(catalog, price=1200):
    return {"submission_key": uuid.uuid4().hex, "buyer_instagram": "buyer",
            "items": [{"product_id": catalog.product.id, "quantity": 1, "expected_price_jpy": price}]}


def assert_public(response):
    output = response.get_data(as_text=True)
    for secret in ("recordcity.jp", "source_url", "detail_error_code", "supplier_private_error", "private.example", "supplier.invalid", '"site"', '"last_price"'):
        assert secret not in output


def test_unchecked_stock_is_visible_but_never_orderable(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    page = client.get(f"/catalog/{catalog.pricelist.token}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Stock not checked" in html and "Check availability" in html
    assert "/media/product_images/record.jpg" in html
    button = re.search(r'<button[^>]+data-request-product-id="\d+"[^>]*>', html).group()
    assert " disabled" not in button
    assert_public(page)
    response = client.get(endpoint(catalog))
    assert response.json["status"] == "none"
    assert response.json["item"]["stock"] == 0
    assert response.json["item"]["in_stock"] is False
    assert catalog.dispatched == []
    rejected = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog))
    assert rejected.status_code == 409
    assert rejected.json["code"] == "items_unavailable"
    assert db_session.query(CatalogRequest).count() == 0


def test_public_start_deduplicates_and_poll_never_enqueues(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    first = client.post(endpoint(catalog))
    assert first.status_code == 202
    assert first.json["status"] == "pending"
    assert first.json["retry_after_seconds"] == 3
    assert_public(first)
    assert len(catalog.dispatched) == 1
    assert catalog.dispatched[0][0:2] == (catalog.product.id, catalog.owner.id)
    assert catalog.dispatched[0][-1] == catalog.shop.id
    assert client.post(endpoint(catalog)).status_code == 202
    assert client.get(endpoint(catalog)).status_code == 202
    assert len(catalog.dispatched) == 1
    db_session.refresh(catalog.product)
    assert catalog.product.detail_fetch_state == "queued"


def test_ready_details_refresh_price_before_user_can_submit(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    assert client.post(endpoint(catalog)).status_code == 202
    db_session.refresh(catalog.product)
    catalog.product.detail_fetch_state = "complete"
    catalog.product.last_status = "on_sale"
    catalog.product.selling_price = 1500
    catalog.product.variants[0].inventory_qty = 2
    db_session.commit()
    poll = client.get(endpoint(catalog))
    assert poll.status_code == 200
    assert poll.json["status"] == "ready"
    assert poll.json["item"]["price"] == 1500
    assert poll.json["item"]["stock"] == 2
    stale = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog))
    assert stale.status_code == 409 and stale.json["code"] == "catalog_changed"
    accepted = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog, 1500))
    assert accepted.status_code == 201
    assert db_session.query(CatalogRequest).count() == 1
    assert len(catalog.dispatched) == 1


@pytest.mark.parametrize("state", [None, "complete"])
def test_legacy_or_completed_stock_is_immediately_available(client, db_session, deferred_catalog, state):
    catalog = deferred_catalog
    catalog.product.detail_fetch_state = state
    catalog.product.last_status = "on_sale"
    db_session.commit()
    assert client.post(endpoint(catalog)).json["status"] == "ready"
    assert catalog.dispatched == []
    response = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog))
    assert response.status_code == 201


def test_explicitly_sold_product_has_no_enabled_add_or_detail_fetch(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    catalog.product.last_status = "sold"
    db_session.commit()
    response = client.post(endpoint(catalog))
    assert response.status_code == 200 and response.json["status"] == "unavailable"
    assert response.json["item"]["stock"] == 0
    assert catalog.dispatched == []
    html = client.get(f"/catalog/{catalog.pricelist.token}").get_data(as_text=True)
    button = re.search(r'<button[^>]+data-request-product-id="\d+"[^>]*>', html).group()
    assert " disabled" in button
    assert "Check availability" not in html


@pytest.mark.parametrize("ledger", ["same_request", "mismatched_source", "mismatched_scope"])
def test_failure_backoff_is_generic_and_cannot_be_bypassed_by_public_request(client, db_session, deferred_catalog, ledger):
    catalog = deferred_catalog
    catalog.product.detail_fetch_state = "failed"
    catalog.product.detail_retry_at = utc_now() + timedelta(minutes=5)
    catalog.product.detail_fail_count = 3
    catalog.product.detail_source_url = catalog.product.source_url if ledger != "mismatched_source" else "https://private.example/secret"
    catalog.product.detail_scope_key = product_detail_jobs._scope_key(catalog.owner.id, catalog.shop.id) if ledger != "mismatched_scope" else None
    db_session.commit()
    response = client.post(endpoint(catalog))
    assert response.status_code == 202 and response.json["status"] == "pending"
    assert 299 <= response.json["retry_after_seconds"] <= 301
    assert catalog.dispatched == []
    assert_public(response)
    db_session.refresh(catalog.product)
    assert catalog.product.detail_fetch_state == "failed"
    assert catalog.product.detail_fail_count == 3
    assert catalog.product.detail_retry_at > utc_now()


def test_public_retry_is_accepted_after_backoff_expires(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    catalog.product.detail_fetch_state = "failed"
    catalog.product.detail_retry_at = utc_now() - timedelta(seconds=1)
    catalog.product.detail_fail_count = 3
    # A stale or incomplete old ledger still cannot prevent an allowed retry.
    catalog.product.detail_source_url = "https://private.example/secret"
    catalog.product.detail_scope_key = None
    db_session.commit()
    response = client.post(endpoint(catalog))
    assert response.status_code == 202 and response.json["status"] == "pending"
    assert len(catalog.dispatched) == 1
    assert_public(response)
    db_session.refresh(catalog.product)
    assert catalog.product.detail_fetch_state == "queued"
    assert catalog.product.detail_source_url == catalog.product.source_url
    assert catalog.product.detail_scope_key == product_detail_jobs._scope_key(catalog.owner.id, catalog.shop.id)
    assert catalog.product.detail_retry_at is None


@pytest.mark.parametrize("corruption", ["hidden", "archived", "deleted", "foreign_owner", "foreign_shop", "different_shop", "inactive", "expired", "suspended", "foreign_list_shop", "removed"])
def test_invalid_public_scope_cannot_poll_or_enqueue(client, db_session, deferred_catalog, corruption):
    catalog = deferred_catalog
    if corruption == "hidden":
        catalog.row.visible = False
    elif corruption == "archived":
        catalog.product.archived = True
    elif corruption == "deleted":
        catalog.product.deleted_at = utc_now()
    elif corruption == "foreign_owner":
        catalog.product.user_id = catalog.other.id
    elif corruption == "foreign_shop":
        catalog.product.shop_id = catalog.other_shop.id
    elif corruption == "different_shop":
        catalog.product.shop_id = catalog.same_owner_shop.id
    elif corruption == "inactive":
        catalog.pricelist.is_active = False
    elif corruption == "expired":
        catalog.pricelist.unpublish_at = utc_now() - timedelta(seconds=1)
    elif corruption == "suspended":
        catalog.owner.suspended_at = utc_now()
    elif corruption == "foreign_list_shop":
        catalog.pricelist.shop_id = catalog.other_shop.id
    else:
        db_session.delete(catalog.row)
    db_session.commit()
    for method in (client.get, client.post):
        response = method(endpoint(catalog))
        assert response.status_code == 404 and response.json == {"error": "Not found"}
    assert catalog.dispatched == []


def test_unbound_legacy_list_and_unscoped_product_remain_eligible(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    catalog.pricelist.shop_id = catalog.product.shop_id = None
    db_session.commit()
    assert client.post(endpoint(catalog)).status_code == 202
    assert len(catalog.dispatched) == 1 and catalog.dispatched[0][-1] is None


@pytest.mark.parametrize("state", ["pending", "complete"])
def test_new_two_stage_product_never_exposes_source_price_as_customer_price(client, db_session, deferred_catalog, state):
    catalog = deferred_catalog
    catalog.product.detail_fetch_state = state
    catalog.product.selling_price = None
    db_session.commit()
    response = client.get(endpoint(catalog))
    assert response.json["item"]["price"] is None
    catalog.row.custom_price = 1900
    db_session.commit()
    assert client.get(endpoint(catalog)).json["item"]["price"] == 1900


def test_completed_staged_product_keeps_cost_private_on_get_add_and_submit(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    private_cost = 987654321
    catalog.product.detail_fetch_state = "complete"
    catalog.product.last_status = "on_sale"
    catalog.product.last_price = private_cost
    catalog.product.selling_price = None
    catalog.product.variants[0].price = private_cost
    catalog.product.variants[0].selling_price = None
    db_session.commit()

    availability = client.get(endpoint(catalog))
    assert availability.json["status"] == "ready"
    assert availability.json["item"]["price"] is None
    detail = client.get(f"/catalog/{catalog.pricelist.token}/product/{catalog.product.id}")
    assert detail.json["price"] is None
    page = client.get(f"/catalog/{catalog.pricelist.token}").get_data(as_text=True)
    config = json.loads(re.search(r'id="catalogRequestConfig">(.*?)</script>', page, re.S).group(1))
    assert config["items"][0]["price"] is None
    assert "Price on request" in page
    assert str(private_cost) not in page

    stale = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog, private_cost))
    assert stale.status_code == 409 and stale.json["code"] == "catalog_changed"
    assert stale.json["items"][0]["price"] is None
    assert str(private_cost) not in stale.get_data(as_text=True)
    assert db_session.query(CatalogRequest).count() == 0
    accepted = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog, None))
    assert accepted.status_code == 201
    assert db_session.query(CatalogRequest).one().items[0].price_jpy_snapshot is None


@pytest.mark.parametrize("price_source,customer_price", [("product", 2100), ("variant", 2200), ("list", 2300)])
def test_completed_staged_product_uses_only_configured_customer_price(
    client, db_session, deferred_catalog, price_source, customer_price,
):
    catalog = deferred_catalog
    catalog.product.detail_fetch_state = "complete"
    catalog.product.last_status = "on_sale"
    catalog.product.selling_price = None
    if price_source == "product":
        catalog.product.selling_price = customer_price
    elif price_source == "variant":
        catalog.product.variants[0].selling_price = customer_price
    else:
        catalog.row.custom_price = customer_price
    db_session.commit()
    assert client.get(endpoint(catalog)).json["item"]["price"] == customer_price
    accepted = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog, customer_price))
    assert accepted.status_code == 201
    assert db_session.query(CatalogRequest).one().items[0].price_jpy_snapshot == customer_price


def test_legacy_null_detail_state_retains_existing_customer_price_fallback(client, db_session, deferred_catalog):
    catalog = deferred_catalog
    catalog.product.detail_fetch_state = None
    catalog.product.last_status = "on_sale"
    catalog.product.selling_price = None
    db_session.commit()
    assert client.get(endpoint(catalog)).json["item"]["price"] == 777
    accepted = client.post(f"/catalog/{catalog.pricelist.token}/requests", json=inquiry(catalog, 777))
    assert accepted.status_code == 201
    assert db_session.query(CatalogRequest).one().items[0].price_jpy_snapshot == 777


def test_public_detail_start_requires_csrf_but_poll_is_read_only(app, client, deferred_catalog, monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    response = client.post(endpoint(deferred_catalog))
    assert response.status_code == 400 and response.json["code"] == "csrf_failed"
    assert client.get(endpoint(deferred_catalog)).status_code == 200
    assert deferred_catalog.dispatched == []


def test_rendered_csrf_token_starts_deferred_details_over_https(app, client, deferred_catalog, monkeypatch):
    catalog = deferred_catalog
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    path = f"/catalog/{catalog.pricelist.token}"
    html = client.get(path, base_url="https://localhost").get_data(as_text=True)
    config = json.loads(re.search(r'id="catalogRequestConfig">(.*?)</script>', html, re.S).group(1))
    response = client.post(endpoint(catalog), base_url="https://localhost",
                           headers={"X-CSRFToken": config["csrf_token"], "Referer": "https://localhost" + path})
    assert response.status_code == 202 and len(catalog.dispatched) == 1


def test_repeated_public_starts_are_rate_limited_and_still_deduplicated(client, deferred_catalog, monkeypatch):
    from routes import catalog as routes
    monkeypatch.setattr(routes, "DETAIL_START_LIMIT", 2)
    assert client.post(endpoint(deferred_catalog)).status_code == 202
    assert client.post(endpoint(deferred_catalog)).status_code == 202
    limited = client.post(endpoint(deferred_catalog))
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "900"
    assert len(deferred_catalog.dispatched) == 1


def test_rate_store_failure_does_not_dispatch_or_leak_details(client, deferred_catalog, monkeypatch):
    from routes import catalog as routes
    def fail():
        raise RuntimeError("https://private.example/secret")
    monkeypatch.setattr(routes, "get_rate_limiter", fail)
    response = client.post(endpoint(deferred_catalog))
    assert response.status_code == 503
    assert deferred_catalog.dispatched == []
    assert_public(response)


def test_owner_start_budget_is_shared_across_tokens_and_clients(client, db_session, deferred_catalog, monkeypatch):
    from routes import catalog as routes
    catalog = deferred_catalog
    second = PriceList(user_id=catalog.owner.id, shop_id=catalog.shop.id, name="Second list", token=uuid.uuid4().hex)
    db_session.add(second)
    db_session.flush()
    db_session.add(PriceListItem(price_list_id=second.id, product_id=catalog.product.id, visible=True))
    db_session.commit()
    monkeypatch.setattr(routes, "DETAIL_OWNER_START_LIMIT", 1)
    assert client.post(endpoint(catalog), headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 202
    other_path = f"/catalog/{second.token}/products/{catalog.product.id}/details"
    assert client.post(other_path, headers={"X-Forwarded-For": "192.0.2.2"}).status_code == 429
    assert len(catalog.dispatched) == 1


def test_poll_budget_never_dispatches_jobs(client, deferred_catalog, monkeypatch):
    from routes import catalog as routes
    monkeypatch.setattr(routes, "DETAIL_POLL_LIMIT", 1)
    assert client.get(endpoint(deferred_catalog)).status_code == 200
    limited = client.get(endpoint(deferred_catalog))
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "300"
    assert deferred_catalog.dispatched == []


def test_queue_failure_is_public_waiting_with_durable_backoff(client, db_session, deferred_catalog, monkeypatch):
    def fail_dispatch(*args):
        raise RuntimeError("https://private.example/secret")
    monkeypatch.setattr(product_detail_jobs, "_dispatch_detail_job", fail_dispatch)
    response = client.post(endpoint(deferred_catalog))
    assert response.status_code == 202 and response.json["status"] == "pending"
    assert 29 <= response.json["retry_after_seconds"] <= 31
    assert_public(response)
    db_session.refresh(deferred_catalog.product)
    assert deferred_catalog.product.detail_fetch_state == "failed"
    assert deferred_catalog.product.detail_retry_at > utc_now()


def test_selected_demand_waiting_for_capacity_remains_public_pending(client, db_session, deferred_catalog, monkeypatch):
    catalog = deferred_catalog
    monkeypatch.setattr(product_detail_jobs, "_OWNER_ACTIVE_LIMIT", 0)
    assert client.get(endpoint(catalog)).json["status"] == "none"
    started = client.post(endpoint(catalog))
    assert started.status_code == 202 and started.json["status"] == "pending"
    assert started.json["retry_after_seconds"] == 30
    assert catalog.dispatched == []
    db_session.refresh(catalog.product)
    assert catalog.product.detail_fetch_state == "pending"
    assert catalog.product.detail_source_url == catalog.product.source_url
    assert client.get(endpoint(catalog)).status_code == 202
    monkeypatch.setattr(product_detail_jobs, "_OWNER_ACTIVE_LIMIT", 1)
    assert product_detail_jobs.recover_product_detail_jobs()["queued"] == 1
    assert len(catalog.dispatched) == 1
