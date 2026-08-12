"""
Customer-facing additions to the public catalog.

The catalog is the page overseas customers actually see, so the things tested
here are the ones that decide whether they get in touch: a way to ask about an
item, a shipping expectation, and prices in a currency they recognise.
"""
import pytest

from models import PriceList, PriceListItem, Product, ProductSnapshot, Shop, User, Variant
from routes.catalog import _normalize_instagram_username, _resolve_catalog_contact
from time_utils import utc_now


def _create_user(db_session, username):
    user = User(username=username, last_login_at=utc_now())
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _build_catalog(
    db_session,
    username,
    *,
    token,
    instagram=None,
    shipping_note=None,
    title_en="English Title",
    attach_shop=True,
):
    user = _create_user(db_session, username)

    shop = None
    if attach_shop:
        shop = Shop(user_id=user.id, name=f"{username}-shop", instagram_username=instagram)
        db_session.add(shop)
        db_session.commit()

    product = Product(
        user_id=user.id,
        shop_id=shop.id if shop else None,
        site="manual",
        source_url=f"https://example.com/{username}",
        last_title="日本語の商品名 高騰中！",
        custom_title="日本語の商品名 高騰中！",
        custom_title_en=title_en,
        last_price=3000,
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()
    db_session.add_all(
        [
            Variant(product_id=product.id, option1_value="Default Title", price=3000,
                    selling_price=6000, inventory_qty=2, position=1),
            ProductSnapshot(product_id=product.id, title="日本語の商品名", price=3000,
                            image_urls="/media/product_images/x.jpg", scraped_at=utc_now()),
        ]
    )
    db_session.commit()

    pricelist = PriceList(
        user_id=user.id,
        shop_id=shop.id if shop else None,
        name="Catalog",
        token=token,
        is_active=True,
        shipping_note=shipping_note,
    )
    db_session.add(pricelist)
    db_session.commit()
    db_session.add(
        PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True, sort_order=0)
    )
    db_session.commit()
    return user, pricelist, product


class TestInstagramNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("my_shop", "my_shop"),
            ("@my_shop", "my_shop"),
            ("  @my.shop  ", "my.shop"),
            ("https://www.instagram.com/my_shop/", "my_shop"),
            ("https://instagram.com/my_shop?hl=ja", "my_shop"),
            ("instagram.com/my_shop", "my_shop"),
            ("http://www.instagram.com/my_shop", "my_shop"),
        ],
    )
    def test_the_usual_shapes_all_reduce_to_the_username(self, raw, expected):
        assert _normalize_instagram_username(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            # A post, a reel and a story name content, not a person.
            "https://www.instagram.com/p/CabcDEF123/",
            "https://www.instagram.com/reel/CabcDEF123/",
            "https://www.instagram.com/stories/my_shop/123456/",
            "https://www.instagram.com/explore/tags/pokemon/",
            "https://www.instagram.com/my_shop/reels/",
            # A different site that merely ends in the same words.
            "https://evilinstagram.com/attacker",
            "https://instagram.com.example.net/attacker",
            "https://instagram.com/",
        ],
    )
    def test_a_url_that_does_not_name_a_profile_is_dropped(self, raw):
        # Guessing here would send customers to a stranger's inbox.
        assert _normalize_instagram_username(raw) == ""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            None,
            "   ",
            "name with spaces",
            "javascript:alert(1)",
            'x" onmouseover="alert(1)',
            "a" * 31,
        ],
    )
    def test_anything_that_is_not_a_username_is_dropped(self, raw):
        # The value goes straight into a link on a public page.
        assert _normalize_instagram_username(raw) == ""


class TestCatalogContactLink:
    def test_the_catalog_offers_a_direct_message_link(self, client, db_session):
        _build_catalog(db_session, "contact_user", token="contact-token", instagram="my_shop")

        html = client.get("/catalog/contact-token").get_data(as_text=True)

        assert "https://ig.me/m/my_shop" in html
        assert "Ask about this item" in html
        assert "Message us on Instagram" in html

    def test_no_handle_means_no_contact_button(self, client, db_session):
        _build_catalog(db_session, "no_contact_user", token="no-contact-token")

        html = client.get("/catalog/no-contact-token").get_data(as_text=True)

        assert "ig.me" not in html
        assert "Ask about this item" not in html

    def test_a_handle_on_the_products_shop_is_found(self, db_session):
        user = _create_user(db_session, "fallback_contact_user")
        shop = Shop(user_id=user.id, name="fallback-shop", instagram_username="from_product")
        db_session.add(shop)
        db_session.commit()
        product = Product(
            user_id=user.id,
            shop_id=shop.id,
            site="manual",
            source_url="https://example.com/fallback",
            last_title="x",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db_session.add(product)
        db_session.commit()
        # The price list itself has no shop, so the item's shop supplies it.
        pricelist = PriceList(user_id=user.id, name="L", token="fallback-token", is_active=True)
        db_session.add(pricelist)
        db_session.commit()
        item = PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True)
        db_session.add(item)
        db_session.commit()

        assert _resolve_catalog_contact(pricelist, [item]) == "from_product"

    def test_another_accounts_shop_is_never_used(self, db_session):
        owner = _create_user(db_session, "contact_owner")
        stranger = _create_user(db_session, "contact_stranger")
        stranger_shop = Shop(
            user_id=stranger.id, name="stranger-shop", instagram_username="not_mine"
        )
        db_session.add(stranger_shop)
        db_session.commit()

        pricelist = PriceList(user_id=owner.id, name="L", token="isolation-token", is_active=True)
        pricelist.shop = stranger_shop
        db_session.add(pricelist)
        db_session.commit()

        assert _resolve_catalog_contact(pricelist, []) == ""


class TestShippingNote:
    def test_the_note_appears_beside_the_prices(self, client, db_session):
        _build_catalog(
            db_session,
            "shipping_user",
            token="shipping-token",
            shipping_note="Shipping: worldwide, tracked",
        )

        html = client.get("/catalog/shipping-token").get_data(as_text=True)

        assert "Shipping: worldwide, tracked" in html

    def test_no_note_means_nothing_is_shown(self, client, db_session):
        _build_catalog(db_session, "no_shipping_user", token="no-shipping-token")

        html = client.get("/catalog/no-shipping-token").get_data(as_text=True)

        assert "product-card-shipping" not in html

    def test_the_note_is_saved_from_the_edit_form(self, client, db_session):
        user = _create_user(db_session, "shipping_form_user")
        client.post("/login", data={"username": "shipping_form_user", "password": "testpassword"})

        client.post(
            "/pricelists/create",
            data={
                "name": "With Shipping",
                "notes": "",
                "currency_rate": "150",
                "layout": "grid",
                "theme": "dark",
                "shop_id": "",
                "is_active": "on",
                "unpublish_at": "",
                "list_type": "permanent",
                "shipping_note": "  Shipping:   worldwide,  tracked  ",
            },
            follow_redirects=True,
        )

        created = db_session.query(PriceList).filter_by(user_id=user.id).one()
        # Runs of whitespace are collapsed so the public page reads cleanly.
        assert created.shipping_note == "Shipping: worldwide, tracked"

    def test_an_empty_note_is_stored_as_nothing(self, client, db_session):
        user = _create_user(db_session, "shipping_empty_user")
        client.post("/login", data={"username": "shipping_empty_user", "password": "testpassword"})

        client.post(
            "/pricelists/create",
            data={
                "name": "No Shipping",
                "notes": "",
                "currency_rate": "150",
                "layout": "grid",
                "theme": "dark",
                "shop_id": "",
                "is_active": "on",
                "unpublish_at": "",
                "list_type": "permanent",
                "shipping_note": "   ",
            },
            follow_redirects=True,
        )

        created = db_session.query(PriceList).filter_by(user_id=user.id).one()
        assert created.shipping_note is None


class TestEnglishTitleWarning:
    def test_items_without_an_english_title_are_flagged(self, client, db_session):
        user, pricelist, _product = _build_catalog(
            db_session, "en_missing_user", token="en-missing-token", title_en=None
        )
        client.post("/login", data={"username": "en_missing_user", "password": "testpassword"})

        html = client.get(f"/pricelists/{pricelist.id}/items").get_data(as_text=True)

        assert "英語の商品名が未設定の商品が 1商品あります" in html
        assert "未設定 → 付ける" in html

    def test_no_warning_when_every_item_has_one(self, client, db_session):
        _user, pricelist, _product = _build_catalog(
            db_session, "en_present_user", token="en-present-token", title_en="English Title"
        )
        client.post("/login", data={"username": "en_present_user", "password": "testpassword"})

        html = client.get(f"/pricelists/{pricelist.id}/items").get_data(as_text=True)

        assert "英語の商品名が未設定の商品が" not in html


# The words also appear in the page's own JavaScript, so these match the
# server-rendered badge markup rather than searching the whole document.
IN_STOCK_BADGE = '<span class="stock-badge in-stock">In Stock</span>'
OUT_OF_STOCK_BADGE = '<span class="stock-badge out-of-stock">Out of Stock</span>'


class TestStockDisplay:
    def test_an_item_in_stock_shows_its_quantity(self, client, db_session):
        _build_catalog(db_session, "stock_user", token="stock-token")

        html = client.get("/catalog/stock-token").get_data(as_text=True)

        assert IN_STOCK_BADGE in html
        assert "Qty: 2" in html
        assert OUT_OF_STOCK_BADGE not in html

    def test_an_item_with_no_stock_never_claims_to_be_available(self, client, db_session):
        _user, _pricelist, product = _build_catalog(
            db_session, "no_stock_user", token="no-stock-token"
        )
        for variant in product.variants:
            variant.inventory_qty = 0
        db_session.commit()

        html = client.get("/catalog/no-stock-token").get_data(as_text=True)

        assert OUT_OF_STOCK_BADGE in html
        assert IN_STOCK_BADGE not in html
        assert 'class="stock-qty"' not in html
