"""
The pre-listing checklist on the product edit screen.

The header chip only revealed what was missing on hover, which never happens on
a touch screen, so the detail is now on the page itself.
"""
import re

from models import Product, User, Variant
from time_utils import utc_now


def _login(client, db_session, username):
    user = User(username=username, last_login_at=utc_now())
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


def _add_product(db_session, user, *, title=None, description=None, with_variant=False, price=None):
    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/checklist/{user.id}",
        last_title=title,
        custom_title=title,
        custom_description=description,
        last_price=1000,
        status="draft",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()
    if with_variant:
        db_session.add(
            Variant(
                product_id=product.id,
                option1_value="Default Title",
                price=1000,
                selling_price=price,
                inventory_qty=1,
                position=1,
            )
        )
        db_session.commit()
    return product


class TestInlineChecklist:
    def test_an_incomplete_product_says_how_many_items_are_left(self, client, db_session):
        user = _login(client, db_session, "checklist_todo_user")
        product = _add_product(db_session, user)

        html = client.get(f"/product/{product.id}").get_data(as_text=True)

        assert 'id="productChecklistCard"' in html
        assert 'data-ready="todo"' in html
        assert "出品前チェック：あと" in html

    def test_the_missing_items_name_what_to_do(self, client, db_session):
        user = _login(client, db_session, "checklist_reason_user")
        product = _add_product(db_session, user)

        html = client.get(f"/product/{product.id}").get_data(as_text=True)

        assert "日本語の商品名を入力してください" in html
        assert "販売価格を入力してください" in html
        assert "バリエーションを1件以上追加してください" in html

    def test_each_item_links_to_the_panel_that_fixes_it(self, client, db_session):
        user = _login(client, db_session, "checklist_anchor_user")
        product = _add_product(db_session, user)

        html = client.get(f"/product/{product.id}").get_data(as_text=True)
        card = html.split('id="productChecklistCard"')[1].split("</section>")[0]

        assert 'href="#productImagesPanel"' in card
        assert 'href="#productDescriptionPanel"' in card
        assert 'href="#productVariantPanel"' in card
        assert 'href="#productSalesPanel"' in card

    def test_a_complete_product_reports_it_is_ready(self, client, db_session):
        user = _login(client, db_session, "checklist_done_user")
        product = _add_product(
            db_session,
            user,
            title="完成した商品",
            description="状態は良好です",
            with_variant=True,
            price=3000,
        )
        snapshot_images = "https://example.com/a.jpg"
        from models import ProductSnapshot

        db_session.add(
            ProductSnapshot(
                product_id=product.id,
                title="完成した商品",
                price=1000,
                image_urls=snapshot_images,
                scraped_at=utc_now(),
            )
        )
        db_session.commit()

        html = client.get(f"/product/{product.id}").get_data(as_text=True)

        assert "出品前チェック：すべて入力できています" in html
        assert re.search(r'id="productChecklistCard"[^>]*data-ready="ok"', html)


class TestHeaderChipWording:
    def test_the_javascript_keeps_the_full_wording(self, client, db_session):
        user = _login(client, db_session, "checklist_wording_user")
        product = _add_product(db_session, user)

        html = client.get(f"/product/{product.id}").get_data(as_text=True)

        # The live update used to rewrite the chip as the shorter "チェック".
        assert "'チェック ' + completedCount" not in html
        assert "'出品前チェック ' + completedCount" in html
