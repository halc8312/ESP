"""
Cost / sale price / profit columns on the admin screens.

Selling below cost is the mistake the school's students most need to catch, so
the profit figure and its loss styling are asserted directly on the rendered
HTML rather than only on the numbers behind it.
"""
import re

from models import User, Product, Variant, PriceList, PriceListItem
from time_utils import utc_now


def _login(client, db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


def _add_product(db_session, user, title, cost, selling_price):
    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/{title}",
        last_title=title,
        last_price=cost,
        last_status="on_sale",
        selling_price=selling_price,
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()
    db_session.add(
        Variant(
            product_id=product.id,
            option1_value="Default Title",
            price=cost,
            selling_price=selling_price,
            inventory_qty=1,
            position=1,
        )
    )
    db_session.commit()
    return product


class TestProductListProfitColumn:
    def test_product_list_splits_cost_sale_and_profit_into_columns(self, client, db_session):
        user = _login(client, db_session, "profit_columns_user")
        _add_product(db_session, user, "Profitable", 12000, 30000)

        html = client.get("/").get_data(as_text=True)

        assert "仕入価格" in html
        assert "販売価格" in html
        assert "利益" in html
        assert "¥12,000" in html
        assert "¥30,000" in html
        # 30,000 - 12,000 = +18,000 (+150%)
        assert "+¥18,000" in html
        assert "+150%" in html

    def test_product_list_flags_a_sale_price_below_cost(self, client, db_session):
        user = _login(client, db_session, "profit_loss_user")
        _add_product(db_session, user, "Loss Making", 36100, 22333)

        html = client.get("/").get_data(as_text=True)

        assert "profit--minus" in html
        assert "−¥13,767" in html
        assert "（赤字）" in html

    def test_product_list_links_unpriced_products_to_the_edit_screen(self, client, db_session):
        user = _login(client, db_session, "profit_unset_user")
        product = _add_product(db_session, user, "No Price Yet", 8000, None)

        html = client.get("/").get_data(as_text=True)

        assert "未設定 → 決める" in html
        assert re.search(
            r'<a class="money-unset" href="[^"]*/product/%d"' % product.id,
            html,
        )


class TestPriceListItemsProfitColumn:
    def test_price_list_items_warns_about_items_priced_below_cost(self, client, db_session):
        user = _login(client, db_session, "pricelist_profit_user")
        losing = _add_product(db_session, user, "Below Cost", 20000, 15000)
        earning = _add_product(db_session, user, "Above Cost", 5000, 9000)

        pricelist = PriceList(
            user_id=user.id,
            name="Main List",
            token="profit-columns-token",
            is_active=True,
        )
        db_session.add(pricelist)
        db_session.commit()
        db_session.add_all(
            [
                PriceListItem(price_list_id=pricelist.id, product_id=losing.id, visible=True, sort_order=0),
                PriceListItem(price_list_id=pricelist.id, product_id=earning.id, visible=True, sort_order=1),
            ]
        )
        db_session.commit()

        html = client.get(f"/pricelists/{pricelist.id}/items").get_data(as_text=True)

        assert "利益がマイナスの商品が 1商品あります" in html
        assert "profit--minus" in html
        assert "−¥5,000" in html
        assert "+¥4,000" in html

    def test_price_list_items_has_no_warning_when_every_item_earns(self, client, db_session):
        user = _login(client, db_session, "pricelist_no_loss_user")
        earning = _add_product(db_session, user, "All Good", 5000, 9000)

        pricelist = PriceList(
            user_id=user.id,
            name="Main List",
            token="profit-columns-clean-token",
            is_active=True,
        )
        db_session.add(pricelist)
        db_session.commit()
        db_session.add(
            PriceListItem(price_list_id=pricelist.id, product_id=earning.id, visible=True, sort_order=0)
        )
        db_session.commit()

        html = client.get(f"/pricelists/{pricelist.id}/items").get_data(as_text=True)

        assert "利益がマイナスの商品が" not in html
        assert "profit--minus" not in html


class TestPricingRulesProfitColumn:
    def test_pricing_rules_show_the_profit_of_the_worked_example(self, client, db_session):
        _login(client, db_session, "pricing_profit_user")

        client.post(
            "/pricing/create",
            data={
                "name": "標準利益率",
                "margin_rate": "30",
                "shipping_cost": "0",
                "fixed_fee": "0",
            },
            follow_redirects=True,
        )

        html = client.get("/pricing").get_data(as_text=True)

        # (1000 + 0) * 1.30 + 0 = 1300 -> +300 (+30%)
        assert "¥1,300" in html
        assert "+¥300" in html
        assert "+30%" in html
