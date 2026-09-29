"""An owner can correct a paused patrol without restarting bad URL retries."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from models import PriceList, PriceListItem, Product, Shop, User, Variant
from time_utils import utc_now


@pytest.fixture
def paused_product(client, db_session):
    owner = User(username="paused_patrol_owner", last_login_at=utc_now())
    owner.set_password("testpassword")
    other = User(username="paused_patrol_other")
    other.set_password("testpassword")
    db_session.add_all([owner, other])
    db_session.flush()
    shop = Shop(user_id=owner.id, name="Owner Shop")
    db_session.add(shop)
    db_session.flush()
    product = Product(
        user_id=owner.id,
        shop_id=shop.id,
        site="snkrdunk",
        source_url="https://snkrdunk.com/search/?q=shoes",
        last_title="Saved product",
        custom_title="Saved product",
        last_price=4500,
        last_status="on_sale",
        selling_price=6000,
        status="active",
        patrol_fail_count=124,
        last_patrolled_at=utc_now() - timedelta(hours=2),
        patrol_paused_reason="invalid_url",
        patrol_paused_source_url="https://snkrdunk.com/search/?q=shoes",
    )
    db_session.add(product)
    db_session.flush()
    variant = Variant(product_id=product.id, inventory_qty=7, price=4500, position=1)
    db_session.add(variant)
    db_session.commit()
    client.post("/login", data={"username": owner.username, "password": "testpassword"})
    return SimpleNamespace(product=product, variant=variant, owner=owner, other=other, shop=shop)


def _edit_data(paused, **overrides):
    return {
        "title": "Saved product",
        "status": "active",
        "shop_id": str(paused.shop.id),
        "resume_patrol": "on",
        "patrol_source_url": "https://snkrdunk.com/products/DD1391-100/",
        **overrides,
    }


def test_owner_sees_pause_reason_and_explicit_resume_control(client, paused_product):
    response = client.get(f"/product/{paused_product.product.id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="productPatrolPausedNotice"' in html
    assert "対応する商品ページとして確認できませんでした" in html
    assert "最後に取得できた情報" in html
    assert 'name="patrol_source_url"' in html
    assert 'name="resume_patrol"' in html
    assert 'name="csrf_token"' in html


@pytest.mark.parametrize("url", [
    "https://snkrdunk.com/products/DD1391-100/",
    "https://snkrdunk.com/apparels/12345/",
    "https://snkrdunk.com/apparels/12345/used/67890/",
])
def test_correct_supported_url_resumes_next_patrol_without_erasing_history(
    client, db_session, paused_product, url
):
    product = paused_product.product
    last_patrolled_at = product.last_patrolled_at
    response = client.post(f"/product/{product.id}", data=_edit_data(paused_product, patrol_source_url=url))
    assert response.status_code == 302
    db_session.refresh(product)
    db_session.refresh(paused_product.variant)
    assert product.source_url == url
    assert product.patrol_paused_reason is None
    assert product.patrol_paused_source_url is None
    assert product.next_patrol_at is None
    assert product.patrol_fail_count == 124
    assert product.last_patrolled_at == last_patrolled_at
    assert (product.last_price, product.last_status, product.selling_price) == (4500, "on_sale", 6000)
    assert paused_product.variant.inventory_qty == 7
    assert product.shop_id == paused_product.shop.id


@pytest.mark.parametrize("url", [
    "https://snkrdunk.com/search/?q=shoes",  # same known-invalid URL
    "https://snkrdunk.com/apparels/not-a-product/",
    "https://snkrdunk.com/products/DD1391-100/used/123/",
    "https://jp.mercari.com/item/m12345",  # changing source site is forbidden
    "https://snkrdunk.com.evil.example/products/DD1391-100/",
    "https://127.0.0.1/products/DD1391-100/",
    "http://snkrdunk.com/products/DD1391-100/",
    "https://user:secret@snkrdunk.com/products/DD1391-100/",
    "https://snkrdunk.com:8443/products/DD1391-100/",
    "",
])
def test_invalid_resume_is_rejected_without_other_product_edits(
    client, db_session, paused_product, url
):
    product = paused_product.product
    original_url = product.source_url
    response = client.post(
        f"/product/{product.id}",
        data=_edit_data(paused_product, patrol_source_url=url, title="Must not be saved"),
    )
    assert response.status_code == 400
    assert "巡回を再開できません" in response.get_data(as_text=True)
    db_session.refresh(product)
    assert product.source_url == original_url
    assert product.patrol_paused_source_url == original_url
    assert product.patrol_paused_reason == "invalid_url"
    assert product.patrol_fail_count == 124
    assert product.custom_title == "Saved product"


@pytest.mark.parametrize("original_url", ["https://snkrdunk.com/search/?q=shoes", "not-a-url"])
def test_regular_edit_keeps_pause_and_source_url(client, db_session, paused_product, original_url):
    product = paused_product.product
    product.source_url = original_url
    product.patrol_paused_source_url = original_url
    db_session.commit()
    response = client.post(
        f"/product/{product.id}",
        data=_edit_data(paused_product, resume_patrol="", title="Edited title"),
    )
    assert response.status_code == 302
    db_session.refresh(product)
    assert product.custom_title == "Edited title"
    assert product.source_url == original_url
    assert product.patrol_paused_reason == "invalid_url"
    assert product.patrol_fail_count == 124


def test_url_newly_supported_by_policy_can_resume_without_string_change(client, db_session, paused_product):
    product = paused_product.product
    product.source_url = "https://snkrdunk.com/apparels/12345/used/67890/"
    product.patrol_paused_source_url = product.source_url
    db_session.commit()
    response = client.post(
        f"/product/{product.id}", data=_edit_data(paused_product, patrol_source_url=product.source_url)
    )
    assert response.status_code == 302
    db_session.refresh(product)
    assert product.patrol_paused_reason is None
    assert product.patrol_fail_count == 124


@pytest.mark.parametrize("corrected_url", [
    "https://snkrdunk.com/apparels/12345/",  # parent is not the same used listing
    "https://snkrdunk.com/apparels/12345/used/67891/",
    "https://snkrdunk.com/products/DD1391-100/",
])
def test_known_product_identity_cannot_change_on_resume(client, db_session, paused_product, corrected_url):
    product = paused_product.product
    original_url = "http://snkrdunk.com/apparels/12345/used/67890/"
    product.source_url = original_url
    product.patrol_paused_source_url = original_url
    db_session.commit()
    response = client.post(
        f"/product/{product.id}", data=_edit_data(paused_product, patrol_source_url=corrected_url)
    )
    assert response.status_code == 400
    db_session.refresh(product)
    assert product.source_url == original_url
    assert product.patrol_paused_reason == "invalid_url"


def test_known_product_identity_allows_safe_url_correction(client, db_session, paused_product):
    product = paused_product.product
    product.source_url = "http://snkrdunk.com/apparels/12345/used/67890/"
    product.patrol_paused_source_url = product.source_url
    db_session.commit()
    corrected_url = "https://www.snkrdunk.com/apparels/12345/used/67890/"
    response = client.post(
        f"/product/{product.id}", data=_edit_data(paused_product, patrol_source_url=corrected_url)
    )
    assert response.status_code == 302
    db_session.refresh(product)
    assert product.source_url == corrected_url
    assert product.patrol_paused_reason is None


@pytest.mark.parametrize("same_shop", [True, False])
def test_existing_product_url_in_registration_scope_cannot_be_reused(
    client, db_session, paused_product, same_shop
):
    url = "https://snkrdunk.com/products/DD1391-100/"
    existing = Product(
        user_id=paused_product.owner.id,
        shop_id=paused_product.shop.id if same_shop else None,
        site="snkrdunk",
        source_url=url,
    )
    db_session.add(existing)
    db_session.commit()
    response = client.post(f"/product/{paused_product.product.id}", data=_edit_data(paused_product))
    assert response.status_code == 400
    assert "すでに登録されています" in response.get_data(as_text=True)
    db_session.refresh(paused_product.product)
    assert paused_product.product.patrol_paused_reason == "invalid_url"
    assert paused_product.product.source_url != url


def test_other_users_matching_product_url_does_not_block_correction(client, db_session, paused_product):
    existing = Product(
        user_id=paused_product.other.id,
        site="snkrdunk",
        source_url="https://snkrdunk.com/products/DD1391-100/",
    )
    db_session.add(existing)
    db_session.commit()
    response = client.post(f"/product/{paused_product.product.id}", data=_edit_data(paused_product))
    assert response.status_code == 302
    db_session.refresh(paused_product.product)
    assert paused_product.product.patrol_paused_reason is None


def test_other_user_cannot_view_or_resume_patrol(client, db_session, paused_product):
    client.get("/logout")
    client.post("/login", data={"username": paused_product.other.username, "password": "testpassword"})
    path = f"/product/{paused_product.product.id}"
    assert client.get(path).status_code == 404
    assert client.post(path, data=_edit_data(paused_product)).status_code == 404
    db_session.refresh(paused_product.product)
    assert paused_product.product.patrol_paused_reason == "invalid_url"


def test_foreign_shop_rejected_without_resuming(client, db_session, paused_product):
    foreign_shop = Shop(user_id=paused_product.other.id, name="Other Shop")
    db_session.add(foreign_shop)
    db_session.commit()
    response = client.post(
        f"/product/{paused_product.product.id}", data=_edit_data(paused_product, shop_id=str(foreign_shop.id))
    )
    assert response.status_code == 400
    db_session.refresh(paused_product.product)
    assert paused_product.product.patrol_paused_reason == "invalid_url"
    assert paused_product.product.shop_id == paused_product.shop.id


def test_resume_requires_csrf_token(app, client, db_session, paused_product, monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    response = client.post(f"/product/{paused_product.product.id}", data=_edit_data(paused_product))
    assert response.status_code == 400
    db_session.refresh(paused_product.product)
    assert paused_product.product.patrol_paused_reason == "invalid_url"


def test_public_catalog_does_not_expose_patrol_fields_or_source(client, db_session, paused_product):
    pricelist = PriceList(user_id=paused_product.owner.id, name="Public list", token="paused-public-list")
    db_session.add(pricelist)
    db_session.flush()
    db_session.add(PriceListItem(price_list_id=pricelist.id, product_id=paused_product.product.id, visible=True))
    db_session.commit()
    client.get("/logout")
    response = client.get(f"/catalog/{pricelist.token}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert paused_product.product.source_url not in html
    assert "productPatrolPausedNotice" not in html
    assert "patrol_source_url" not in html
    assert "resume_patrol" not in html
