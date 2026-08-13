from models import (
    PriceList,
    PriceListItem,
    Product,
    ProductSnapshot,
    Shop,
    User,
    Variant,
)
from services.product_service import save_scraped_items_to_db
from time_utils import utc_now


def _create_user(db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username):
    return client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )


class _CompletedQueue:
    def __init__(self, user_id, result):
        self.user_id = user_id
        self.result = result

    def get_status(self, job_id, user_id=None):
        if job_id != "tenant-job" or user_id != self.user_id:
            return None
        return {
            "job_id": job_id,
            "status": "completed",
            "result": self.result,
            "error": None,
        }


class _CaptureQueue:
    def __init__(self):
        self.enqueue_kwargs = None

    def enqueue(self, **kwargs):
        self.enqueue_kwargs = kwargs
        return "captured-job"


def test_logout_then_other_user_login_cannot_reuse_previous_shop_for_write(
    client,
    db_session,
):
    user_a = _create_user(db_session, "tenant_session_user_a")
    user_b = _create_user(db_session, "tenant_session_user_b")
    shop_a = Shop(user_id=user_a.id, name="Tenant A Shop")
    db_session.add(shop_a)
    db_session.commit()

    _login(client, user_a.username)
    client.post("/set_current_shop", data={"shop_id": str(shop_a.id)})

    client.get("/logout")
    with client.session_transaction() as browser_session:
        assert "current_shop_id" not in browser_session
        # Simulate a legacy/stale session created before the logout fix.
        browser_session["current_shop_id"] = shop_a.id

    _login(client, user_b.username)
    with client.session_transaction() as browser_session:
        assert "current_shop_id" not in browser_session

    response = client.post(
        "/products/manual-add",
        data={
            "title": "Tenant B Unscoped Product",
            "cost_price": "1000",
            "inventory_qty": "1",
            "stock_state": "on_sale",
            "publish_status": "draft",
            "site": "manual",
        },
    )

    assert response.status_code == 302
    product = db_session.query(Product).filter_by(
        user_id=user_b.id,
        last_title="Tenant B Unscoped Product",
    ).one()
    assert product.shop_id is None
    assert product.shop_id != shop_a.id


def test_registration_auto_login_clears_stale_shop_session(client, db_session):
    owner = _create_user(db_session, "registration_stale_shop_owner")
    foreign_shop = Shop(user_id=owner.id, name="Registration Stale Shop")
    db_session.add(foreign_shop)
    db_session.commit()

    with client.session_transaction() as browser_session:
        browser_session["current_shop_id"] = foreign_shop.id

    response = client.post(
        "/register",
        data={
            "username": "registration_clean_session_user",
            "password": "safe-registration-password-42",
            "password_confirm": "safe-registration-password-42",
        },
    )

    assert response.status_code == 302
    with client.session_transaction() as browser_session:
        assert "current_shop_id" not in browser_session


def test_product_service_drops_foreign_shop_context(client, db_session):
    owner = _create_user(db_session, "service_foreign_shop_owner")
    target_user = _create_user(db_session, "service_foreign_shop_target")
    foreign_shop = Shop(user_id=owner.id, name="Service Foreign Shop")
    db_session.add(foreign_shop)
    db_session.commit()

    result = save_scraped_items_to_db(
        [
            {
                "url": "https://jp.mercari.com/item/m-tenant-service",
                "title": "Tenant Service Product",
                "price": 2000,
                "status": "on_sale",
                "description": "",
                "image_urls": [],
            }
        ],
        user_id=target_user.id,
        site="mercari",
        shop_id=foreign_shop.id,
    )

    assert result == (1, 0)
    product = db_session.query(Product).filter_by(user_id=target_user.id).one()
    assert product.shop_id is None


def test_product_edit_rejects_explicit_foreign_shop_without_partial_update(
    client,
    db_session,
):
    owner = _create_user(db_session, "product_edit_shop_owner")
    target_user = _create_user(db_session, "product_edit_shop_target")
    foreign_shop = Shop(user_id=owner.id, name="Product Edit Foreign Shop")
    db_session.add(foreign_shop)
    product = Product(
        user_id=target_user.id,
        site="manual",
        source_url="https://example.invalid/product-edit-tenant",
        last_title="Original Tenant Product",
        last_price=1200,
        last_status="on_sale",
        custom_title="Original Tenant Product",
    )
    db_session.add(product)
    db_session.commit()
    _login(client, target_user.username)

    response = client.post(
        f"/product/{product.id}",
        data={
            "shop_id": str(foreign_shop.id),
            "title": "MUTATED TITLE",
        },
    )

    assert response.status_code == 400
    assert "選択したショップが見つかりません" in response.get_data(as_text=True)
    db_session.expire_all()
    persisted = db_session.query(Product).filter_by(id=product.id).one()
    assert persisted.shop_id is None
    assert persisted.custom_title == "Original Tenant Product"


def test_scrape_run_clears_stale_foreign_session_before_queueing(
    client,
    db_session,
    monkeypatch,
):
    owner = _create_user(db_session, "scrape_session_shop_owner")
    target_user = _create_user(db_session, "scrape_session_shop_target")
    foreign_shop = Shop(user_id=owner.id, name="Scrape Session Foreign Shop")
    db_session.add(foreign_shop)
    db_session.commit()
    _login(client, target_user.username)

    with client.session_transaction() as browser_session:
        browser_session["current_shop_id"] = foreign_shop.id

    queue = _CaptureQueue()
    monkeypatch.setattr("routes.scrape.get_queue", lambda: queue)
    monkeypatch.setattr(
        "routes.scrape._build_scrape_task",
        lambda **_kwargs: lambda: {"items": []},
    )

    response = client.post(
        "/scrape/run",
        data={
            "site": "mercari",
            "keyword": "tenant boundary",
            "response_mode": "preview",
        },
    )

    assert response.status_code == 202
    assert queue.enqueue_kwargs["request_payload"]["shop_id"] is None
    with client.session_transaction() as browser_session:
        assert "current_shop_id" not in browser_session


def test_queued_foreign_shop_cannot_scope_product_or_new_pricelist(
    client,
    db_session,
    monkeypatch,
):
    owner = _create_user(db_session, "queued_foreign_shop_owner")
    target_user = _create_user(db_session, "queued_foreign_shop_target")
    foreign_shop = Shop(user_id=owner.id, name="Queued Foreign Shop")
    db_session.add(foreign_shop)
    db_session.commit()
    _login(client, target_user.username)

    queue = _CompletedQueue(
        target_user.id,
        {
            "site": "mercari",
            "shop_id": foreign_shop.id,
            "items": [
                {
                    "url": "https://jp.mercari.com/item/m-queued-tenant",
                    "title": "Queued Tenant Product",
                    "price": 2500,
                    "status": "on_sale",
                    "description": "",
                    "image_urls": ["/media/product_images/queued-local.jpg"],
                }
            ],
        },
    )
    monkeypatch.setattr("routes.scrape.get_queue", lambda: queue)

    response = client.post(
        "/scrape/register-to-pricelist",
        json={
            "job_id": "tenant-job",
            "selected_indices": [0],
            "new_list_name": "Tenant Safe List",
            "translate": False,
        },
    )

    assert response.status_code == 200
    product = db_session.query(Product).filter_by(user_id=target_user.id).one()
    price_list = db_session.query(PriceList).filter_by(user_id=target_user.id).one()
    assert product.shop_id is None
    assert price_list.shop_id is None


def test_public_catalog_uses_only_curated_text_and_local_images_and_owned_branding(
    client,
    db_session,
):
    owner = _create_user(db_session, "catalog_boundary_owner")
    foreign_user = _create_user(db_session, "catalog_boundary_foreign")
    foreign_shop = Shop(
        user_id=foreign_user.id,
        name="FOREIGN BRAND SECRET",
        logo_url="/media/shop_logos/foreign-secret.png",
    )
    db_session.add(foreign_shop)
    db_session.flush()

    product = Product(
        user_id=owner.id,
        shop_id=foreign_shop.id,
        site="mercari",
        source_url="https://jp.mercari.com/item/m-catalog-boundary",
        last_title="Safe fallback title",
        last_price=3000,
        last_status="on_sale",
        custom_description="<p>Curated Japanese copy</p>",
        custom_description_en="<p>Curated English copy</p>",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        Variant(
            product_id=product.id,
            option1_value="Default Title",
            price=3000,
            inventory_qty=1,
            position=1,
        )
    )
    snapshot = ProductSnapshot(
        product_id=product.id,
        title="Raw source title",
        price=3000,
        status="on_sale",
        description="RAW SOURCE DESCRIPTION SECRET",
        image_urls=(
            "https://static.mercdn.net/item/detail/orig/photos/m123_1.jpg"
            "|/media/product_images/catalog-local.jpg"
            "|/static/images/catalog-static.jpg"
        ),
        scraped_at=utc_now(),
    )
    db_session.add(snapshot)

    price_list = PriceList(
        user_id=owner.id,
        shop_id=foreign_shop.id,
        name="Safe Catalog",
        token="catalog-boundary-token",
        layout="list",
        is_active=True,
    )
    db_session.add(price_list)
    db_session.flush()
    db_session.add(
        PriceListItem(
            price_list_id=price_list.id,
            product_id=product.id,
            visible=True,
        )
    )
    db_session.commit()

    page = client.get(f"/catalog/{price_list.token}")
    detail = client.get(f"/catalog/{price_list.token}/product/{product.id}")

    assert page.status_code == 200
    assert detail.status_code == 200
    html = page.get_data(as_text=True)
    payload = detail.get_json()

    for public_output in (html, detail.get_data(as_text=True)):
        assert "static.mercdn.net" not in public_output
        assert "RAW SOURCE DESCRIPTION SECRET" not in public_output
        assert "FOREIGN BRAND SECRET" not in public_output
        assert "foreign-secret.png" not in public_output

    assert "/media/product_images/catalog-local.jpg" in html
    assert "Safe fallback title" in html
    assert "Curated English copy" in html
    assert payload["image_urls"] == [
        "/media/product_images/catalog-local.jpg",
        "/static/images/catalog-static.jpg",
    ]
    assert payload["description_text"] == "Curated Japanese copy"
    assert payload["description_en_text"] == "Curated English copy"
    assert payload["title"] == "Safe fallback title"
    assert snapshot.image_urls.startswith("https://static.mercdn.net/")


def test_public_catalog_hides_corrupt_cross_tenant_item(client, db_session):
    owner = _create_user(db_session, "catalog_item_owner")
    foreign_user = _create_user(db_session, "catalog_item_foreign")
    foreign_product = Product(
        user_id=foreign_user.id,
        site="manual",
        source_url="https://example.invalid/foreign-product",
        last_title="FOREIGN PRODUCT SECRET",
        last_price=100,
        last_status="on_sale",
        status="active",
    )
    db_session.add(foreign_product)
    db_session.flush()
    price_list = PriceList(
        user_id=owner.id,
        name="Owner Catalog",
        token="cross-tenant-item-token",
        is_active=True,
    )
    db_session.add(price_list)
    db_session.flush()
    db_session.add(
        PriceListItem(
            price_list_id=price_list.id,
            product_id=foreign_product.id,
            visible=True,
        )
    )
    db_session.commit()

    page = client.get(f"/catalog/{price_list.token}")
    detail = client.get(
        f"/catalog/{price_list.token}/product/{foreign_product.id}"
    )

    assert page.status_code == 200
    assert "FOREIGN PRODUCT SECRET" not in page.get_data(as_text=True)
    assert detail.status_code == 404
