"""Public image polling is read-only, bounded, and token/shop scoped."""
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest

from models import PriceList, PriceListItem, Product, ProductSnapshot, ProductThumbnailJob, Shop, User, Variant
from time_utils import utc_now


@pytest.fixture
def catalog(db_session, monkeypatch):
    owner = User(username="thumbnail-catalog-owner", password_hash="test")
    other = User(username="thumbnail-catalog-other", password_hash="test")
    db_session.add_all([owner, other])
    db_session.flush()
    shop = Shop(user_id=owner.id, name="Owner shop")
    own_other_shop = Shop(user_id=owner.id, name="Another own shop")
    foreign_shop = Shop(user_id=other.id, name="Foreign shop")
    db_session.add_all([shop, own_other_shop, foreign_shop])
    db_session.flush()
    product = Product(
        user_id=owner.id, shop_id=shop.id, site="recordcity",
        source_url="https://www.recordcity.jp/ja/catalog/123456",
        last_title="Public title", last_price=777, selling_price=1200,
        last_status="unknown", detail_fetch_state="pending", detail_error_code="private-error",
        variants=[Variant(price=777, inventory_qty=0)],
    )
    pricelist = PriceList(user_id=owner.id, shop_id=shop.id, name="Public list", token=uuid.uuid4().hex)
    db_session.add_all([product, pricelist])
    db_session.flush()
    snapshot = ProductSnapshot(product_id=product.id, title="Title", price=777, status="unknown", image_urls="https://files.recordcity.jp/private.jpg")
    item = PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True)
    db_session.add_all([snapshot, item])
    db_session.flush()
    job = ProductThumbnailJob(
        product_id=product.id, user_id=owner.id, shop_id=shop.id,
        product_source_url=product.source_url, source_snapshot_id=snapshot.id,
        source_image_url=snapshot.image_urls, state="queued", job_id="private-batch-id",
        claim_token="private-claim-token", lease_expires_at=utc_now() + timedelta(minutes=20),
    )
    db_session.add(job)
    db_session.commit()
    monkeypatch.setattr("services.product_detail_jobs.enqueue_product_details", lambda *a, **kw: pytest.fail("GET must not enqueue details"))
    monkeypatch.setattr("services.product_thumbnail_jobs.recover_thumbnail_jobs", lambda *a, **kw: pytest.fail("GET must not recover/enqueue images"))
    return SimpleNamespace(owner=owner, other=other, shop=shop, own_other_shop=own_other_shop,
                           foreign_shop=foreign_shop, product=product, pricelist=pricelist,
                           snapshot=snapshot, item=item, job=job)


def endpoint(catalog, ids=None, token=None):
    token = catalog.pricelist.token if token is None else token
    ids = str(catalog.product.id) if ids is None else ids
    return f"/catalog/{token}/thumbnails?product_ids={ids}"


def assert_public(response):
    output = response.get_data(as_text=True)
    for secret in ("recordcity", "source_url", "private.jpg", "private-error", "claim_token", "private-claim-token", "private-batch-id", "price", "stock", "inventory", '"site"'):
        assert secret not in output


def test_pending_poll_has_only_safe_image_fields_and_does_not_mutate_jobs(client, db_session, catalog):
    response = client.get(endpoint(catalog))
    assert response.status_code == 200
    assert response.json == {"items": [{"product_id": catalog.product.id, "status": "pending", "thumb_url": ""}]}
    assert response.headers["Cache-Control"] == "no-store"
    assert_public(response)
    db_session.expire_all()
    assert catalog.job.state == "queued"
    assert catalog.job.job_id == "private-batch-id"
    assert catalog.job.attempts == 0
    assert catalog.product.detail_fetch_state == "pending"
    assert catalog.product.variants[0].inventory_qty == 0


def test_readonly_poll_observes_image_completion_without_price_or_availability_changes(client, db_session, catalog):
    assert client.get(endpoint(catalog)).json["items"][0]["status"] == "pending"
    catalog.snapshot.image_urls = "/media/product-delivery/thumbnail/1/opaque-token/0.png"
    catalog.job.state = "complete"
    catalog.job.managed_image_url = catalog.snapshot.image_urls
    db_session.commit()
    response = client.get(endpoint(catalog))
    assert response.json["items"] == [{"product_id": catalog.product.id, "status": "ready", "thumb_url": catalog.snapshot.image_urls}]
    assert_public(response)
    assert catalog.product.detail_fetch_state == "pending"
    assert catalog.product.last_status == "unknown"


@pytest.mark.parametrize("state", ["pending", "queued", "running", "failed", "complete"])
def test_only_inflight_image_states_remain_pending(client, db_session, catalog, state):
    catalog.job.state = state
    db_session.commit()
    expected = "pending" if state in {"pending", "queued", "running"} else "unavailable"
    assert client.get(endpoint(catalog)).json["items"][0]["status"] == expected


@pytest.mark.parametrize("drift", ["owner", "shop", "source", "snapshot", "image"])
def test_stale_image_demand_is_not_publicly_pending(client, db_session, catalog, drift):
    if drift == "owner":
        catalog.job.user_id = catalog.other.id
    elif drift == "shop":
        catalog.job.shop_id = catalog.own_other_shop.id
    elif drift == "source":
        catalog.job.product_source_url = "https://private.example/changed"
    elif drift == "snapshot":
        newer = ProductSnapshot(product_id=catalog.product.id, image_urls="https://files.recordcity.jp/new.jpg", scraped_at=utc_now() + timedelta(seconds=1))
        db_session.add(newer)
    elif drift == "image":
        catalog.job.source_image_url = "https://files.recordcity.jp/changed.jpg"
    db_session.commit()
    response = client.get(endpoint(catalog))
    assert response.json["items"][0]["status"] == "unavailable"
    assert_public(response)


@pytest.mark.parametrize("drift", ["invisible", "foreign_owner", "other_shop", "foreign_shop", "shop_transferred", "list_shop_transferred", "archived", "deleted", "inactive", "expired", "suspended", "unknown_token", "unknown_product"])
def test_token_and_visible_owner_shop_scope_is_enforced(client, db_session, catalog, drift):
    token, ids = None, None
    if drift == "invisible":
        catalog.item.visible = False
    elif drift == "foreign_owner":
        catalog.product.user_id = catalog.other.id
    elif drift == "other_shop":
        catalog.product.shop_id = catalog.own_other_shop.id
    elif drift == "foreign_shop":
        catalog.pricelist.shop_id = None
        catalog.product.shop_id = catalog.foreign_shop.id
    elif drift in {"shop_transferred", "list_shop_transferred"}:
        catalog.shop.user_id = catalog.other.id
    elif drift == "archived":
        catalog.product.archived = True
    elif drift == "deleted":
        catalog.product.deleted_at = utc_now()
    elif drift == "inactive":
        catalog.pricelist.is_active = False
    elif drift == "expired":
        catalog.pricelist.unpublish_at = utc_now() - timedelta(seconds=1)
    elif drift == "suspended":
        catalog.owner.suspended_at = utc_now()
    elif drift == "unknown_token":
        token = "unknown"
    elif drift == "unknown_product":
        ids = "987654"
    db_session.commit()
    response = client.get(endpoint(catalog, ids=ids, token=token))
    assert response.status_code == 404
    assert response.json == {"error": "Not found"}
    assert_public(response)


def test_unscoped_owned_legacy_product_is_supported_without_starting_work(client, db_session, catalog):
    catalog.pricelist.shop_id = None
    catalog.product.shop_id = None
    catalog.product.detail_fetch_state = None
    catalog.snapshot.image_urls = "/media/old-product.png"
    db_session.commit()
    response = client.get(endpoint(catalog))
    assert response.status_code == 200
    assert response.json["items"][0]["thumb_url"] == "/media/old-product.png"


@pytest.mark.parametrize("ids", ["", "0", "-1", "true", "1.5", "1,,2", "99999999999999999999999999999999", ",".join(["1"] * 51)])
def test_invalid_or_oversized_batches_fail_before_reading_products(client, catalog, ids):
    assert client.get(endpoint(catalog, ids=ids)).status_code == 400


def test_batch_is_one_read_request_deduplicates_and_rejects_mixed_visibility(client, db_session, catalog):
    response = client.get(endpoint(catalog, ids=f"{catalog.product.id},{catalog.product.id}"))
    assert len(response.json["items"]) == 1
    assert client.get(endpoint(catalog, ids=f"{catalog.product.id},987654")).status_code == 404


def test_fifty_images_are_read_with_scope_and_latest_snapshot_in_one_select(client, db_session, catalog):
    from sqlalchemy import event
    products = [catalog.product]
    for index in range(49):
        product = Product(user_id=catalog.owner.id, shop_id=catalog.shop.id, site="recordcity", source_url=f"https://www.recordcity.jp/ja/catalog/{2000 + index}")
        db_session.add(product)
        db_session.flush()
        db_session.add_all([
            PriceListItem(price_list_id=catalog.pricelist.id, product_id=product.id, visible=True),
            ProductSnapshot(product_id=product.id, image_urls=f"/media/owned-{index}.png"),
        ])
        products.append(product)
    db_session.commit()
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get(endpoint(catalog, ids=",".join(str(product.id) for product in products)))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert len(response.json["items"]) == 50
    image_reads = [statement for statement in statements if "product_snapshots" in statement]
    assert len(image_reads) == 1
    assert all("update " not in statement and "insert " not in statement and "delete " not in statement for statement in statements)
    assert_public(response)


def test_scope_revocation_between_token_lookup_and_batch_read_is_rechecked(client, db_session, catalog, monkeypatch):
    import routes.catalog as routes
    from database import create_isolated_session
    real_limiter = routes.get_rate_limiter()

    class TransferBeforeRead:
        def increment(self, *args):
            session = create_isolated_session()
            try:
                session.query(PriceList).filter(PriceList.id == catalog.pricelist.id).update({PriceList.user_id: catalog.other.id})
                session.commit()
            finally:
                session.close()
            return real_limiter.increment(*args)

    monkeypatch.setattr(routes, "get_rate_limiter", lambda: TransferBeforeRead())
    assert client.get(endpoint(catalog)).status_code == 404


def test_readonly_get_is_csrf_independent_and_post_is_not_supported(client, catalog):
    client.application.config["WTF_CSRF_ENABLED"] = True
    assert client.get(endpoint(catalog)).status_code == 200
    client.application.config["WTF_CSRF_ENABLED"] = False
    assert client.post(endpoint(catalog)).status_code == 405


def test_image_poll_budget_is_separate_from_detail_budget(client, catalog, monkeypatch):
    from routes import catalog as routes
    monkeypatch.setattr(routes, "THUMBNAIL_POLL_LIMIT", 1)
    assert client.get(endpoint(catalog)).status_code == 200
    limited = client.get(endpoint(catalog))
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "300"
    assert client.get(f"/catalog/{catalog.pricelist.token}/products/{catalog.product.id}/details").status_code == 200


def test_image_poll_rate_store_failure_is_generic_and_fail_closed(client, catalog, monkeypatch):
    monkeypatch.setattr("routes.catalog.get_rate_limiter", lambda: (_ for _ in ()).throw(RuntimeError("https://private.example/source")))
    response = client.get(endpoint(catalog))
    assert response.status_code == 503
    assert response.json == {"error": "Images are temporarily unavailable."}
    assert_public(response)


@pytest.mark.parametrize("unsafe", ["https://files.recordcity.jp/private.jpg", "//files.recordcity.jp/private.jpg", "/media/../../private", "/media/%2e%2e/private"])
def test_public_poll_never_returns_external_or_unsafe_image_paths(client, db_session, catalog, unsafe):
    catalog.snapshot.image_urls = unsafe
    catalog.job.state = "failed"
    db_session.commit()
    response = client.get(endpoint(catalog))
    assert response.json["items"][0] == {"product_id": catalog.product.id, "status": "unavailable", "thumb_url": ""}
    assert_public(response)


def test_catalog_config_advertises_only_the_token_scoped_refresh_endpoint(client, catalog):
    html = client.get(f"/catalog/{catalog.pricelist.token}").get_data(as_text=True)
    assert f"/catalog/{catalog.pricelist.token}/thumbnails" in html
