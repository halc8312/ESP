from __future__ import annotations

import csv
import io

from bs4 import BeautifulSoup

from models import (
    PriceList,
    PriceListItem,
    PricingRule,
    Product,
    ProductSnapshot,
    User,
    Variant,
)
from services.pricing_service import (
    calculate_selling_price,
    resolve_product_display_price,
    resolve_product_selling_price,
    resolve_variant_selling_price,
    resolve_variant_selling_prices,
)
from time_utils import utc_now


def _login_with_product(client, db_session, username: str):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()

    product = Product(
        user_id=user.id,
        site="mercari",
        source_url=f"https://jp.mercari.com/item/{username}",
        last_title="Price consistency product",
        last_price=1000,
        last_status="on_sale",
        selling_price=2400,
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.flush()
    variant = Variant(
        product_id=product.id,
        option1_value="Default Title",
        price=1000,
        inventory_qty=1,
        position=1,
    )
    snapshot = ProductSnapshot(
        product_id=product.id,
        title=product.last_title,
        price=1000,
        status="on_sale",
        scraped_at=utc_now(),
    )
    db_session.add_all([variant, snapshot])
    db_session.commit()

    client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )
    return user, product, variant


def _single_variant_edit_payload(product, variant, visible_price, **overrides):
    payload = {
        "title": product.last_title,
        "description": "",
        "title_en": "",
        "description_en": "",
        "status": "active",
        "v_ids": [str(variant.id)],
        f"v_opt1_{variant.id}": variant.option1_value or "Default Title",
        f"v_price_{variant.id}": str(visible_price),
        f"v_qty_{variant.id}": str(variant.inventory_qty or 0),
    }
    payload.update(overrides)
    return payload


def test_sale_price_resolver_prefers_product_and_keeps_legacy_fallback():
    product = Product(selling_price=2400)
    assert (
        resolve_product_selling_price(
            product,
            1000,
            fallback_multiplier=1.5,
        )
        == 2400
    )

    product.selling_price = None
    assert (
        resolve_product_selling_price(
            product,
            1000,
            fallback_multiplier=1.5,
        )
        == 1500
    )


def test_pricing_calculation_does_not_turn_missing_cost_into_free_price():
    rule = PricingRule(
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )

    assert calculate_selling_price(None, rule) is None
    assert calculate_selling_price(0, rule) is None
    assert calculate_selling_price(-1, rule) is None


def test_variant_sale_price_resolver_preserves_explicit_zero():
    product = Product(selling_price=2400)
    first = Variant(price=1000, selling_price=0)
    second = Variant(price=1500)

    assert resolve_variant_selling_price(product, first) == 0
    assert resolve_variant_selling_prices(product, [first, second]) == [0, 3600]
    assert resolve_product_display_price(product, [first, second]) == 0


def test_shopify_export_uses_authoritative_sale_price(client, db_session):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "shopify_authoritative_price_user",
    )

    response = client.get(f"/export/shopify?id={product.id}&markup=1.5")

    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert rows[0]["Variant Price"] == "2400"


def test_shopify_export_preserves_multi_variant_price_relationship(
    client,
    db_session,
):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "shopify_multi_variant_price_user",
    )
    db_session.add(
        Variant(
            product_id=product.id,
            option1_value="Large",
            price=1500,
            inventory_qty=1,
            position=2,
        )
    )
    db_session.commit()

    response = client.get(f"/export/shopify?id={product.id}&markup=1.5")

    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert [row["Variant Price"] for row in rows] == ["2400", "3600"]


def test_multi_variant_edit_renders_and_exports_exact_sale_prices(
    client,
    db_session,
):
    _, product, first = _login_with_product(
        client,
        db_session,
        "multi_variant_exact_price_user",
    )
    second = Variant(
        product_id=product.id,
        option1_value="Large",
        price=1500,
        inventory_qty=2,
        position=2,
    )
    db_session.add(second)
    db_session.commit()

    page = client.get(f"/product/{product.id}")
    assert page.status_code == 200
    document = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    assert document.select_one(f'[name="v_price_{first.id}"]')["value"] == "2400"
    assert document.select_one(f'[name="v_price_{second.id}"]')["value"] == "3600"

    response = client.post(
        f"/product/{product.id}",
        data={
            "title": product.last_title,
            "description": "",
            "title_en": "",
            "description_en": "",
            "status": "active",
            "option1_name": "Size",
            "v_ids": [str(first.id), str(second.id)],
            f"v_opt1_{first.id}": "Default Title",
            f"v_price_{first.id}": "2500",
            f"v_qty_{first.id}": "1",
            f"v_opt1_{second.id}": "Large",
            f"v_price_{second.id}": "3550",
            f"v_qty_{second.id}": "2",
        },
    )

    assert response.status_code == 302
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_first = db_session.get(Variant, first.id)
    stored_second = db_session.get(Variant, second.id)
    assert stored_product.selling_price == 2500
    assert stored_first.price == 1000
    assert stored_second.price == 1500
    assert stored_first.selling_price == 2500
    assert stored_second.selling_price == 3550

    shopify = client.get(f"/export/shopify?id={product.id}&markup=9")
    price_update = client.get(f"/export_price_update?id={product.id}&markup=9")
    assert shopify.status_code == 200
    assert price_update.status_code == 200
    shopify_rows = list(
        csv.DictReader(io.StringIO(shopify.get_data(as_text=True)))
    )
    update_rows = list(
        csv.DictReader(io.StringIO(price_update.get_data(as_text=True)))
    )
    assert [row["Variant Price"] for row in shopify_rows] == ["2500", "3550"]
    assert [row["Variant Price"] for row in update_rows] == ["2500", "3550"]

    refreshed_page = client.get(f"/product/{product.id}")
    refreshed_document = BeautifulSoup(
        refreshed_page.get_data(as_text=True),
        "html.parser",
    )
    assert (
        refreshed_document.select_one(f'[name="v_price_{first.id}"]')["value"]
        == "2500"
    )
    assert (
        refreshed_document.select_one(f'[name="v_price_{second.id}"]')["value"]
        == "3550"
    )


def test_legacy_multi_variant_noop_save_keeps_markup_fallback(
    client,
    db_session,
):
    _, product, first = _login_with_product(
        client,
        db_session,
        "legacy_multi_noop_price_user",
    )
    product.selling_price = None
    second = Variant(
        product_id=product.id,
        option1_value="Large",
        price=1500,
        inventory_qty=1,
        position=2,
    )
    db_session.add(second)
    db_session.commit()

    page = client.get(f"/product/{product.id}")
    document = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    first_visible = document.select_one(f'[name="v_price_{first.id}"]')["value"]
    second_visible = document.select_one(f'[name="v_price_{second.id}"]')["value"]
    assert [first_visible, second_visible] == ["1000", "1500"]

    response = client.post(
        f"/product/{product.id}",
        data={
            "title": "Only title changed",
            "description": "",
            "title_en": "",
            "description_en": "",
            "status": "active",
            "v_ids": [str(first.id), str(second.id)],
            f"v_opt1_{first.id}": "Default Title",
            f"v_price_{first.id}": first_visible,
            f"v_qty_{first.id}": "1",
            f"v_opt1_{second.id}": "Large",
            f"v_price_{second.id}": second_visible,
            f"v_qty_{second.id}": "1",
        },
    )

    assert response.status_code == 302
    db_session.expire_all()
    assert db_session.get(Product, product.id).selling_price is None
    assert db_session.get(Variant, first.id).selling_price is None
    assert db_session.get(Variant, second.id).selling_price is None

    export = client.get(f"/export_price_update?id={product.id}&markup=1.5")
    rows = list(csv.DictReader(io.StringIO(export.get_data(as_text=True))))
    assert [row["Variant Price"] for row in rows] == ["1500", "2250"]


def test_ebay_export_uses_authoritative_sale_price(client, db_session):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "ebay_authoritative_price_user",
    )

    response = client.get(
        f"/export_ebay?id={product.id}&markup=1.5&rate=100"
    )

    assert response.status_code == 200
    rows = list(
        csv.DictReader(
            io.StringIO(response.get_data(as_text=True).lstrip("\ufeff"))
        )
    )
    assert rows[0]["StartPrice"] == "24.00"


def test_product_save_does_not_revert_sale_price_to_unchanged_variant(
    client,
    db_session,
):
    _, product, variant = _login_with_product(
        client,
        db_session,
        "product_save_authoritative_price_user",
    )

    response = client.post(
        f"/product/{product.id}",
        data={
            "title": product.last_title,
            "description": "",
            "title_en": "",
            "description_en": "",
            "status": "active",
            "v_ids": [str(variant.id)],
            f"v_opt1_{variant.id}": "Default Title",
            f"v_price_{variant.id}": "2400",
            f"v_qty_{variant.id}": "1",
        },
    )

    assert response.status_code == 302
    db_session.expire_all()
    assert db_session.get(Product, product.id).selling_price == 2400
    assert db_session.get(Variant, variant.id).price == 1000
    assert db_session.get(Variant, variant.id).selling_price is None


def test_product_noop_save_does_not_copy_rendered_sale_price_into_variant(
    client,
    db_session,
):
    _, product, variant = _login_with_product(
        client,
        db_session,
        "product_rendered_noop_price_user",
    )

    page = client.get(f"/product/{product.id}")
    assert page.status_code == 200
    document = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    rendered_price = document.select_one("#single_variant_price")["value"]
    rendered_original = document.select_one(
        'input[name="single_variant_display_price_original"]'
    )["value"]
    assert rendered_price == rendered_original == "2400"

    response = client.post(
        f"/product/{product.id}",
        data={
            "title": product.last_title,
            "description": "",
            "title_en": "",
            "description_en": "",
            "status": "active",
            "single_variant_display_price_original": rendered_original,
            "v_ids": [str(variant.id)],
            f"v_opt1_{variant.id}": "Default Title",
            f"v_price_{variant.id}": rendered_price,
            f"v_qty_{variant.id}": "1",
        },
    )

    assert response.status_code == 302
    db_session.expire_all()
    assert db_session.get(Product, product.id).selling_price == 2400
    assert db_session.get(Variant, variant.id).price == 1000


def test_product_save_recalculates_when_only_pricing_rule_changes(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "product_rule_only_recalculation_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Selected on save",
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )
    db_session.add(rule)
    db_session.commit()

    response = client.post(
        f"/product/{product.id}",
        data=_single_variant_edit_payload(
            product,
            variant,
            2400,
            pricing_rule_id=str(rule.id),
        ),
    )

    assert response.status_code == 302
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_variant = db_session.get(Variant, variant.id)
    assert stored_product.pricing_rule_id == rule.id
    assert stored_product.selling_price == 1900
    assert stored_variant.selling_price is None
    assert resolve_variant_selling_price(stored_product, stored_variant) == 1900


def test_product_save_recalculates_when_only_manual_margin_changes(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "product_margin_only_recalculation_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Margin base",
        margin_rate=10,
        shipping_cost=200,
        fixed_fee=50,
    )
    db_session.add(rule)
    db_session.flush()
    product.pricing_rule_id = rule.id
    product.manual_margin_rate = 10
    product.selling_price = 1370
    db_session.commit()

    response = client.post(
        f"/product/{product.id}",
        data=_single_variant_edit_payload(
            product,
            variant,
            1370,
            pricing_rule_id=str(rule.id),
            manual_price_enabled="on",
            manual_margin_rate="25",
            manual_shipping_cost="",
        ),
    )

    assert response.status_code == 302
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_variant = db_session.get(Variant, variant.id)
    assert stored_product.manual_margin_rate == 25
    assert stored_product.manual_shipping_cost is None
    assert stored_product.selling_price == 1550
    assert stored_variant.selling_price is None
    assert resolve_variant_selling_price(stored_product, stored_variant) == 1550


def test_product_save_recalculates_when_only_manual_shipping_changes(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "product_shipping_only_recalculation_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Shipping base",
        margin_rate=20,
        shipping_cost=100,
        fixed_fee=0,
    )
    db_session.add(rule)
    db_session.flush()
    product.pricing_rule_id = rule.id
    product.manual_shipping_cost = 100
    product.selling_price = 1320
    db_session.commit()

    response = client.post(
        f"/product/{product.id}",
        data=_single_variant_edit_payload(
            product,
            variant,
            1320,
            pricing_rule_id=str(rule.id),
            manual_price_enabled="on",
            manual_margin_rate="",
            manual_shipping_cost="500",
        ),
    )

    assert response.status_code == 302
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_variant = db_session.get(Variant, variant.id)
    assert stored_product.manual_margin_rate is None
    assert stored_product.manual_shipping_cost == 500
    assert stored_product.selling_price == 1800
    assert stored_variant.selling_price is None
    assert resolve_variant_selling_price(stored_product, stored_variant) == 1800


def test_product_save_recalculates_when_manual_overrides_are_disabled(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "product_manual_override_clear_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Automatic after clear",
        margin_rate=20,
        shipping_cost=100,
        fixed_fee=0,
    )
    db_session.add(rule)
    db_session.flush()
    product.pricing_rule_id = rule.id
    product.manual_margin_rate = 100
    product.manual_shipping_cost = 1000
    product.selling_price = 4000
    db_session.commit()

    response = client.post(
        f"/product/{product.id}",
        data=_single_variant_edit_payload(
            product,
            variant,
            4000,
            pricing_rule_id=str(rule.id),
        ),
    )

    assert response.status_code == 302
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_variant = db_session.get(Variant, variant.id)
    assert stored_product.manual_margin_rate is None
    assert stored_product.manual_shipping_cost is None
    assert stored_product.selling_price == 1320
    assert stored_variant.selling_price is None
    assert resolve_variant_selling_price(stored_product, stored_variant) == 1320


def test_product_pricing_recalculation_preserves_explicit_zero_variant_override(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "product_explicit_zero_recalculation_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Automatic under zero override",
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )
    db_session.add(rule)
    variant.selling_price = 0
    db_session.commit()

    response = client.post(
        f"/product/{product.id}",
        data=_single_variant_edit_payload(
            product,
            variant,
            0,
            pricing_rule_id=str(rule.id),
        ),
    )

    assert response.status_code == 302
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_variant = db_session.get(Variant, variant.id)
    assert stored_product.selling_price == 1900
    assert stored_variant.selling_price == 0
    assert resolve_variant_selling_price(stored_product, stored_variant) == 0
    assert resolve_product_display_price(stored_product, [stored_variant]) == 0


def test_clearing_primary_override_returns_to_configured_automatic_price(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "clear_primary_override_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Automatic price",
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )
    db_session.add(rule)
    db_session.flush()
    product.pricing_rule_id = rule.id
    product.selling_price = 2400
    variant.selling_price = 2500
    db_session.commit()

    response = client.post(
        f"/product/{product.id}",
        data={
            "title": product.last_title,
            "description": "",
            "title_en": "",
            "description_en": "",
            "status": "active",
            "pricing_rule_id": str(rule.id),
            "v_ids": [str(variant.id)],
            f"v_opt1_{variant.id}": "Default Title",
            f"v_price_{variant.id}": "",
            f"v_qty_{variant.id}": "1",
        },
    )

    assert response.status_code == 302
    db_session.expire_all()
    assert db_session.get(Variant, variant.id).selling_price is None
    assert db_session.get(Product, product.id).selling_price == 1900

    export = client.get(f"/export_price_update?id={product.id}&markup=9")
    rows = list(csv.DictReader(io.StringIO(export.get_data(as_text=True))))
    assert rows[0]["Variant Price"] == "1900"


def test_explicit_zero_survives_pricelist_admin_and_public_catalog(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "pricelist_explicit_zero_user",
    )
    variant.selling_price = 0
    price_list = PriceList(
        user_id=user.id,
        name="Zero Price List",
        token="zero-price-list-token",
        is_active=True,
    )
    db_session.add(price_list)
    db_session.flush()
    item = PriceListItem(
        price_list_id=price_list.id,
        product_id=product.id,
        visible=True,
        custom_price=0,
    )
    db_session.add(item)
    db_session.commit()

    admin_page = client.get(f"/pricelists/{price_list.id}/items")
    assert admin_page.status_code == 200
    admin_document = BeautifulSoup(
        admin_page.get_data(as_text=True),
        "html.parser",
    )
    assert (
        admin_document.select_one(f'[name="custom_price_{item.id}"]')["value"]
        == "0"
    )

    catalog_page = client.get(f"/catalog/{price_list.token}")
    assert catalog_page.status_code == 200
    catalog_document = BeautifulSoup(
        catalog_page.get_data(as_text=True),
        "html.parser",
    )
    card = catalog_document.select_one(
        f'[data-product-id="{product.id}"]'
    )
    assert card["data-jpy-price"] == "0"
    assert card.select_one(".product-card-price")["data-jpy-price"] == "0"

    detail = client.get(
        f"/catalog/{price_list.token}/product/{product.id}"
    )
    assert detail.status_code == 200
    assert detail.get_json()["price"] == 0


def test_missing_price_is_not_presented_as_free_in_public_catalog(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "catalog_missing_price_user",
    )
    product.selling_price = None
    product.last_price = None
    variant.selling_price = None
    variant.price = None
    price_list = PriceList(
        user_id=user.id,
        name="Price on Request List",
        token="price-on-request-token",
        is_active=True,
    )
    db_session.add(price_list)
    db_session.flush()
    db_session.add(
        PriceListItem(
            price_list_id=price_list.id,
            product_id=product.id,
            visible=True,
            custom_price=None,
        )
    )
    db_session.commit()

    catalog_page = client.get(f"/catalog/{price_list.token}")
    assert catalog_page.status_code == 200
    document = BeautifulSoup(
        catalog_page.get_data(as_text=True),
        "html.parser",
    )
    card = document.select_one(f'[data-product-id="{product.id}"]')
    assert card["data-jpy-price"] == ""
    assert card.select_one(".product-card-price").get_text(strip=True) == (
        "Price on request"
    )

    detail = client.get(
        f"/catalog/{price_list.token}/product/{product.id}"
    )
    assert detail.status_code == 200
    assert detail.get_json()["price"] is None


def test_public_catalog_sanitizes_legacy_non_positive_currency_rate(
    client,
    db_session,
):
    user, product, _ = _login_with_product(
        client,
        db_session,
        "catalog_legacy_currency_user",
    )
    price_list = PriceList(
        user_id=user.id,
        name="Legacy Currency List",
        token="legacy-currency-rate-token",
        currency_rate=0,
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

    response = client.get(f"/catalog/{price_list.token}")

    assert response.status_code == 200
    assert "const FALLBACK_JPY_RATE = 150;" in response.get_data(as_text=True)


def test_public_catalog_sanitizes_legacy_pricelist_notes(client, db_session):
    user, product, _ = _login_with_product(
        client,
        db_session,
        "catalog_legacy_notes_user",
    )
    price_list = PriceList(
        user_id=user.id,
        name="Legacy Notes List",
        token="legacy-notes-token",
        notes='<p>Welcome</p><script>alert("unsafe")</script>',
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

    response = client.get(f"/catalog/{price_list.token}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "<p>Welcome</p>" in html
    assert "<script>alert" not in html


def test_product_zero_is_exported_when_variant_has_no_override(
    client,
    db_session,
):
    _, product, variant = _login_with_product(
        client,
        db_session,
        "product_explicit_zero_user",
    )
    product.selling_price = 0
    variant.selling_price = None
    db_session.commit()

    shopify = client.get(f"/export/shopify?id={product.id}&markup=3")
    price_update = client.get(f"/export_price_update?id={product.id}&markup=3")

    assert shopify.status_code == 200
    assert price_update.status_code == 200
    shopify_rows = list(
        csv.DictReader(io.StringIO(shopify.get_data(as_text=True)))
    )
    update_rows = list(
        csv.DictReader(io.StringIO(price_update.get_data(as_text=True)))
    )
    assert shopify_rows[0]["Variant Price"] == "0"
    assert update_rows[0]["Variant Price"] == "0"


def test_exports_reject_missing_price_instead_of_emitting_free_listing(
    client,
    db_session,
):
    _, product, variant = _login_with_product(
        client,
        db_session,
        "missing_export_price_user",
    )
    snapshot = product.snapshots[0]
    product.last_price = None
    product.selling_price = None
    variant.price = None
    variant.selling_price = None
    snapshot.price = None
    db_session.commit()

    shopify = client.get(f"/export/shopify?id={product.id}")
    price_update = client.get(f"/export_price_update?id={product.id}")
    ebay = client.get(f"/export_ebay?id={product.id}")

    for response in (shopify, price_update, ebay):
        assert response.status_code == 400
        assert "販売価格が未設定の商品があります".encode() in response.data
        assert str(product.id).encode() in response.data


def test_ebay_export_rejects_invalid_exchange_rate(client, db_session):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "invalid_ebay_rate_user",
    )

    responses = [
        client.get(f"/export_ebay?id={product.id}&rate=0"),
        client.get(f"/export_ebay?id={product.id}&rate=nan"),
        client.get(f"/export_ebay?id={product.id}&rate=inf"),
    ]

    assert all(response.status_code == 400 for response in responses)
    assert all(
        "為替レートは0より大きい値".encode() in response.data
        for response in responses
    )


def test_bulk_margin_uses_pricing_rule_markup_semantics(client, db_session):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "bulk_markup_semantics_user",
    )

    response = client.post(
        "/api/products/bulk-price",
        json={"ids": [product.id], "mode": "margin", "value": 20},
    )

    assert response.status_code == 200
    db_session.refresh(product)
    assert product.selling_price == 1200


def test_bulk_margin_matches_pricing_rule_limit(client, db_session):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "bulk_markup_limit_user",
    )

    accepted = client.post(
        "/api/products/bulk-price",
        json={"ids": [product.id], "mode": "margin", "value": 500},
    )
    rejected = client.post(
        "/api/products/bulk-price",
        json={"ids": [product.id], "mode": "margin", "value": 501},
    )

    assert accepted.status_code == 200
    assert accepted.get_json()["updated_products"][0]["selling_price"] == 6000
    assert rejected.status_code == 400
    assert "0 <= margin <= 500" in rejected.get_json()["error"]


def test_inline_price_clear_returns_to_configured_automatic_price(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "inline_clear_automatic_price_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Inline reset rule",
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )
    db_session.add(rule)
    db_session.flush()
    product.pricing_rule_id = rule.id
    product.selling_price = 9999
    variant.selling_price = 8888
    db_session.commit()

    response = client.patch(
        f"/api/products/{product.id}/inline-update",
        json={"field": "selling_price", "value": ""},
    )

    assert response.status_code == 200
    assert response.get_json()["value"] == 1900
    db_session.expire_all()
    assert db_session.get(Product, product.id).selling_price == 1900
    assert db_session.get(Variant, variant.id).selling_price is None


def test_bulk_price_reset_returns_to_rule_and_manual_overrides(
    client,
    db_session,
):
    user, product, variant = _login_with_product(
        client,
        db_session,
        "bulk_reset_automatic_price_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Bulk reset rule",
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )
    db_session.add(rule)
    db_session.flush()
    product.pricing_rule_id = rule.id
    product.manual_margin_rate = 50
    product.selling_price = 9999
    variant.selling_price = 8888
    db_session.commit()

    response = client.post(
        "/api/products/bulk-price",
        json={"ids": [product.id], "mode": "reset"},
    )

    assert response.status_code == 200
    assert response.get_json()["updated_products"] == [
        {"id": product.id, "selling_price": 2350}
    ]
    db_session.expire_all()
    assert db_session.get(Product, product.id).selling_price == 2350
    assert db_session.get(Variant, variant.id).selling_price is None


def test_bulk_price_never_commits_a_negative_calculated_price(
    client,
    db_session,
):
    _, product, variant = _login_with_product(
        client,
        db_session,
        "bulk_negative_price_user",
    )
    variant.selling_price = 2500
    db_session.commit()

    fixed_add = client.post(
        "/api/products/bulk-price",
        json={"ids": [product.id], "mode": "fixed_add", "value": -2000},
    )
    margin_plus_fixed = client.post(
        "/api/products/bulk-price",
        json={
            "ids": [product.id],
            "mode": "margin_plus_fixed",
            "value": 0,
            "extra_value": -2000,
        },
    )

    for response in (fixed_add, margin_plus_fixed):
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["updated_count"] == 0
        assert payload["skipped_count"] == 1
        assert payload["skipped_products"][0]["reason"] == (
            "calculated price must be >= 0"
        )
    db_session.expire_all()
    assert db_session.get(Product, product.id).selling_price == 2400
    assert db_session.get(Variant, variant.id).selling_price == 2500


def test_bulk_price_rejects_non_finite_margin(client, db_session):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "bulk_non_finite_margin_user",
    )

    response = client.post(
        "/api/products/bulk-price",
        json={"ids": [product.id], "mode": "margin", "value": "nan"},
    )

    assert response.status_code == 400
    assert "0 <= margin <= 500" in response.get_json()["error"]


def test_product_price_preview_uses_currently_selected_owned_rule(
    client,
    db_session,
):
    user, product, _ = _login_with_product(
        client,
        db_session,
        "selected_rule_price_preview_user",
    )
    persisted_rule = PricingRule(
        user_id=user.id,
        name="Persisted rule",
        margin_rate=10,
        shipping_cost=0,
        fixed_fee=0,
    )
    selected_rule = PricingRule(
        user_id=user.id,
        name="Selected rule",
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )
    db_session.add_all([persisted_rule, selected_rule])
    db_session.flush()
    product.pricing_rule_id = persisted_rule.id
    db_session.commit()

    response = client.post(
        f"/api/products/{product.id}/recalc-price",
        json={
            "pricing_rule_id": selected_rule.id,
            "manual_margin_rate": None,
            "manual_shipping_cost": None,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["rule_id"] == selected_rule.id
    assert payload["selling_price"] == 1900
    db_session.refresh(product)
    assert product.pricing_rule_id == persisted_rule.id


def test_product_price_preview_rejects_another_users_rule(
    client,
    db_session,
):
    _, product, _ = _login_with_product(
        client,
        db_session,
        "foreign_rule_price_preview_user",
    )
    other_user = User(username="foreign_rule_owner")
    other_user.set_password("testpassword")
    db_session.add(other_user)
    db_session.flush()
    foreign_rule = PricingRule(
        user_id=other_user.id,
        name="Foreign rule",
        margin_rate=99,
        shipping_cost=0,
        fixed_fee=0,
    )
    db_session.add(foreign_rule)
    db_session.commit()

    response = client.post(
        f"/api/products/{product.id}/recalc-price",
        json={
            "pricing_rule_id": foreign_rule.id,
            "manual_margin_rate": None,
            "manual_shipping_cost": None,
        },
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_deleting_default_rule_clears_references_but_keeps_sale_price(
    client,
    db_session,
):
    user, product, _ = _login_with_product(
        client,
        db_session,
        "delete_default_pricing_rule_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Default",
        margin_rate=20,
        shipping_cost=0,
        fixed_fee=0,
    )
    db_session.add(rule)
    db_session.flush()
    rule_id = rule.id
    user.default_pricing_rule_id = rule.id
    product.pricing_rule_id = rule.id
    db_session.commit()

    response = client.post(f"/pricing/{rule_id}/delete")

    assert response.status_code == 302
    db_session.expire_all()
    refreshed_user = db_session.query(User).filter_by(id=user.id).one()
    refreshed_product = db_session.query(Product).filter_by(id=product.id).one()
    assert refreshed_user.default_pricing_rule_id is None
    assert refreshed_product.pricing_rule_id is None
    assert refreshed_product.selling_price == 2400
    assert db_session.query(PricingRule).filter_by(id=rule_id).one_or_none() is None
