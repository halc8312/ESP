from datetime import datetime, timedelta
import re

from models import PriceList, PriceListItem, Product, User
from time_utils import utc_now


def _create_user(db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, user):
    return client.post(
        "/login",
        data={"username": user.username, "password": "testpassword"},
    )


def _edit_form(**overrides):
    values = {
        "name": "Scheduled List",
        "notes": "",
        "currency_rate": "150",
        "layout": "grid",
        "theme": "dark",
        "shop_id": "",
        "is_active": "on",
        "unpublish_at": "",
    }
    values.update(overrides)
    return values


def test_pricelist_publication_states_do_not_mutate_manual_active_flag():
    now = datetime(2026, 7, 29, 3, 0)

    active = PriceList(is_active=True)
    scheduled = PriceList(is_active=True, unpublish_at=now + timedelta(minutes=1))
    expired = PriceList(is_active=True, unpublish_at=now)
    inactive = PriceList(is_active=False, unpublish_at=now)

    assert active.publication_state_at(now) == "active"
    assert active.is_publicly_available(now) is True
    assert scheduled.publication_state_at(now) == "scheduled"
    assert scheduled.is_publicly_available(now) is True
    assert expired.publication_state_at(now) == "expired"
    assert expired.is_publicly_available(now) is False
    assert expired.is_active is True
    assert inactive.publication_state_at(now) == "inactive"
    assert inactive.is_publicly_available(now) is False


def test_pricelist_create_converts_japan_time_to_naive_utc(client, db_session):
    user = _create_user(db_session, "scheduled_create_user")
    _login(client, user)

    response = client.post(
        "/pricelists/create",
        data=_edit_form(
            name="Japan Time List",
            unpublish_at="2099-01-02T09:30",
        ),
    )

    assert response.status_code == 302
    db_session.expire_all()
    pricelist = (
        db_session.query(PriceList)
        .filter_by(user_id=user.id, name="Japan Time List")
        .one()
    )
    assert pricelist.unpublish_at == datetime(2099, 1, 2, 0, 30)
    assert pricelist.unpublish_at.tzinfo is None


def test_pricelist_edit_can_clear_scheduled_unpublish(client, db_session):
    user = _create_user(db_session, "scheduled_clear_user")
    pricelist = PriceList(
        user_id=user.id,
        name="Scheduled List",
        token="scheduled-clear-token",
        is_active=True,
        unpublish_at=datetime(2099, 1, 2, 0, 30),
    )
    db_session.add(pricelist)
    db_session.commit()
    _login(client, user)

    response = client.post(
        f"/pricelists/{pricelist.id}/edit",
        data=_edit_form(unpublish_at=""),
    )

    assert response.status_code == 302
    db_session.expire_all()
    refreshed = db_session.get(PriceList, pricelist.id)
    assert refreshed.unpublish_at is None
    assert refreshed.is_active is True


def test_pricelist_edit_rejects_invalid_datetime_without_changing_deadline(
    client,
    db_session,
):
    user = _create_user(db_session, "scheduled_invalid_user")
    original_deadline = datetime(2099, 1, 2, 0, 30)
    pricelist = PriceList(
        user_id=user.id,
        name="Scheduled List",
        token="scheduled-invalid-token",
        is_active=True,
        unpublish_at=original_deadline,
    )
    db_session.add(pricelist)
    db_session.commit()
    _login(client, user)

    response = client.post(
        f"/pricelists/{pricelist.id}/edit",
        data=_edit_form(unpublish_at="2099-02-30T10:00"),
    )

    assert response.status_code == 400
    assert "自動非公開日時を正しく入力してください".encode() in response.data
    db_session.expire_all()
    refreshed = db_session.get(PriceList, pricelist.id)
    assert refreshed.unpublish_at == original_deadline
    assert refreshed.is_active is True


def test_pricelist_edit_error_preserves_submitted_unpublished_checkbox(
    client,
    db_session,
):
    user = _create_user(db_session, "scheduled_checkbox_user")
    pricelist = PriceList(
        user_id=user.id,
        name="Published List",
        token="scheduled-checkbox-token",
        is_active=True,
    )
    db_session.add(pricelist)
    db_session.commit()
    _login(client, user)
    form = _edit_form(unpublish_at="2020-01-02T09:30")
    form.pop("is_active")

    response = client.post(
        f"/pricelists/{pricelist.id}/edit",
        data=form,
    )

    assert response.status_code == 400
    checkbox = re.search(
        r'<input[^>]*name="is_active"[^>]*>',
        response.get_data(as_text=True),
    )
    assert checkbox is not None
    assert "checked" not in checkbox.group(0)
    db_session.expire_all()
    assert db_session.get(PriceList, pricelist.id).is_active is True


def test_pricelist_create_rejects_past_datetime_and_preserves_input(
    client,
    db_session,
):
    user = _create_user(db_session, "scheduled_past_user")
    _login(client, user)

    response = client.post(
        "/pricelists/create",
        data=_edit_form(
            name="入力を保持する価格表",
            notes="入力済みの備考",
            unpublish_at="2020-01-02T09:30",
        ),
    )

    assert response.status_code == 400
    assert "現在より後の日時".encode() in response.data
    assert "入力を保持する価格表".encode() in response.data
    assert "入力済みの備考".encode() in response.data
    assert (
        db_session.query(PriceList)
        .filter_by(user_id=user.id, name="入力を保持する価格表")
        .one_or_none()
        is None
    )


def test_pricelist_create_rejects_invalid_currency_and_preserves_input(
    client,
    db_session,
):
    user = _create_user(db_session, "invalid_currency_create_user")
    _login(client, user)

    response = client.post(
        "/pricelists/create",
        data=_edit_form(
            name="入力保持リスト",
            notes="換算レート確認用",
            currency_rate="not-a-number",
        ),
    )

    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "通貨換算レートは1以上の整数で入力してください" in html
    assert 'value="入力保持リスト"' in html
    assert "換算レート確認用" in html
    assert 'name="currency_rate" value="not-a-number"' in html
    assert (
        db_session.query(PriceList)
        .filter_by(user_id=user.id, name="入力保持リスト")
        .one_or_none()
        is None
    )


def test_pricelist_edit_validation_is_atomic_and_preserves_submission(
    client,
    db_session,
):
    user = _create_user(db_session, "invalid_pricelist_edit_user")
    original_deadline = datetime(2099, 1, 2, 0, 30)
    pricelist = PriceList(
        user_id=user.id,
        name="Original List",
        notes="Original notes",
        token="invalid-pricelist-edit-token",
        currency_rate=150,
        layout="grid",
        theme="dark",
        is_active=True,
        unpublish_at=original_deadline,
    )
    db_session.add(pricelist)
    db_session.commit()
    _login(client, user)
    form = _edit_form(
        name="",
        notes="Submitted notes",
        currency_rate="0",
        layout="list",
        theme="light",
        unpublish_at="2099-04-05T12:30",
    )
    form.pop("is_active")

    response = client.post(
        f"/pricelists/{pricelist.id}/edit",
        data=form,
    )

    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "名前を入力してください" in html
    assert "Submitted notes" in html
    assert 'name="currency_rate" value="0"' in html
    assert 'name="layout" value="list" checked' in html
    assert 'name="theme" value="light" checked' in html
    assert 'value="2099-04-05T12:30"' in html
    checkbox = re.search(r'<input[^>]*name="is_active"[^>]*>', html)
    assert checkbox is not None
    assert "checked" not in checkbox.group(0)

    db_session.expire_all()
    refreshed = db_session.get(PriceList, pricelist.id)
    assert refreshed.name == "Original List"
    assert refreshed.notes == "Original notes"
    assert refreshed.currency_rate == 150
    assert refreshed.layout == "grid"
    assert refreshed.theme == "dark"
    assert refreshed.is_active is True
    assert refreshed.unpublish_at == original_deadline


def test_pricelist_item_validation_is_atomic_and_preserves_raw_values(
    client,
    db_session,
):
    user = _create_user(db_session, "invalid_item_price_user")
    products = [
        Product(
            user_id=user.id,
            site="manual",
            source_url=f"https://example.com/item-{index}",
            last_title=f"Item {index}",
            last_price=1000 + index,
        )
        for index in range(2)
    ]
    pricelist = PriceList(
        user_id=user.id,
        name="Atomic Items",
        token="atomic-item-price-token",
        is_active=True,
    )
    db_session.add_all([*products, pricelist])
    db_session.commit()
    items = [
        PriceListItem(
            price_list_id=pricelist.id,
            product_id=products[0].id,
            custom_price=1100,
            visible=True,
            sort_order=0,
        ),
        PriceListItem(
            price_list_id=pricelist.id,
            product_id=products[1].id,
            custom_price=1200,
            visible=False,
            sort_order=1,
        ),
    ]
    db_session.add_all(items)
    db_session.commit()
    _login(client, user)

    response = client.post(
        f"/pricelists/{pricelist.id}/items",
        data={
            "action": "update_items",
            f"custom_price_{items[0].id}": "9999",
            f"visible_{items[0].id}": "on",
            f"custom_price_{items[1].id}": "-1",
            f"visible_{items[1].id}": "on",
        },
    )

    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "カスタム価格は0以上の整数で入力してください" in html
    assert 'value="9999"' in html
    assert 'value="-1"' in html

    db_session.expire_all()
    refreshed_items = [
        db_session.get(PriceListItem, item.id)
        for item in items
    ]
    assert refreshed_items[0].custom_price == 1100
    assert refreshed_items[0].visible is True
    assert refreshed_items[1].custom_price == 1200
    assert refreshed_items[1].visible is False


def test_pricelist_edit_cannot_change_another_users_schedule(client, db_session):
    owner = _create_user(db_session, "scheduled_owner")
    attacker = _create_user(db_session, "scheduled_attacker")
    original_deadline = datetime(2099, 1, 2, 0, 30)
    pricelist = PriceList(
        user_id=owner.id,
        name="Owner List",
        token="scheduled-owner-token",
        is_active=True,
        unpublish_at=original_deadline,
    )
    db_session.add(pricelist)
    db_session.commit()
    _login(client, attacker)

    response = client.post(
        f"/pricelists/{pricelist.id}/edit",
        data=_edit_form(unpublish_at="2099-03-04T12:00"),
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/pricelists")
    db_session.expire_all()
    refreshed = db_session.get(PriceList, pricelist.id)
    assert refreshed.unpublish_at == original_deadline


def test_public_catalog_allows_future_schedule_and_denies_reached_deadline(
    client,
    db_session,
    monkeypatch,
):
    now = utc_now()
    user = _create_user(db_session, "scheduled_catalog_user")
    product = Product(
        user_id=user.id,
        site="manual",
        source_url="https://example.com/scheduled-product",
        last_title="Scheduled Product",
        last_price=1000,
    )
    pricelist = PriceList(
        user_id=user.id,
        name="Scheduled Catalog",
        token="scheduled-catalog-token",
        is_active=True,
        unpublish_at=now + timedelta(minutes=1),
    )
    db_session.add_all([product, pricelist])
    db_session.commit()
    item = PriceListItem(
        price_list_id=pricelist.id,
        product_id=product.id,
        visible=True,
    )
    db_session.add(item)
    db_session.commit()
    monkeypatch.setattr("routes.catalog.utc_now", lambda: now)

    page_response = client.get(f"/catalog/{pricelist.token}")
    detail_response = client.get(
        f"/catalog/{pricelist.token}/product/{product.id}"
    )

    assert page_response.status_code == 200
    assert detail_response.status_code == 200

    pricelist.unpublish_at = now
    db_session.commit()

    expired_page_response = client.get(f"/catalog/{pricelist.token}")
    expired_detail_response = client.get(
        f"/catalog/{pricelist.token}/product/{product.id}"
    )

    assert expired_page_response.status_code == 404
    assert expired_detail_response.status_code == 404
    persisted_pricelist = db_session.get(PriceList, pricelist.id)
    assert persisted_pricelist is not None
    assert persisted_pricelist.is_active is True
    assert db_session.get(Product, product.id) is not None


def test_pricelist_list_shows_scheduled_and_expired_states(client, db_session):
    now = utc_now()
    user = _create_user(db_session, "scheduled_status_user")
    db_session.add_all(
        [
            PriceList(
                user_id=user.id,
                name="Future List",
                token="scheduled-future-status-token",
                is_active=True,
                unpublish_at=now + timedelta(days=1),
            ),
            PriceList(
                user_id=user.id,
                name="Past List",
                token="scheduled-past-status-token",
                is_active=True,
                unpublish_at=now - timedelta(days=1),
            ),
        ]
    )
    db_session.commit()
    _login(client, user)

    response = client.get("/pricelists")

    assert response.status_code == 200
    assert "自動で非公開".encode() in response.data
    assert "公開終了済み".encode() in response.data
