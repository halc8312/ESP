"""
The "other site" reader's route.

Whatever happens, the operator ends up on the manual add form: filled in as far
as extraction got, and told what is left. There is no dead end.
"""
from models import User
from services import generic_product_fetch
from time_utils import utc_now


def _login(client, db_session, username):
    user = User(username=username, last_login_at=utc_now())
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"読み取れた商品","description":"状態は良好",
 "image":"https://cdn.example.com/a.jpg",
 "offers":{"@type":"Offer","price":"4800","priceCurrency":"JPY"}}
</script>
</head><body></body></html>
"""


class TestOtherSiteEntryPoint:
    def test_the_scrape_form_offers_the_other_site_tab(self, client, db_session):
        _login(client, db_session, "other_tab_user")

        html = client.get("/scrape").get_data(as_text=True)

        assert "その他のサイト（自動判定）" in html
        assert "/scrape/other-site" in html


class TestOtherSiteRoute:
    def test_a_readable_page_prefills_the_manual_form(self, client, db_session, monkeypatch):
        _login(client, db_session, "other_ok_user")
        monkeypatch.setattr(
            generic_product_fetch,
            "_fetch_html",
            lambda url: (JSON_LD_PAGE, url),
        )

        response = client.post(
            "/scrape/other-site",
            data={"target_url": "https://shop.example.com/item/1"},
        )
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "読み取り結果" in html
        assert "読み取れた商品" in html
        assert "4800" in html
        assert "https://cdn.example.com/a.jpg" in html
        assert "https://shop.example.com/item/1" in html

    def test_missing_fields_are_named_on_the_page(self, client, db_session, monkeypatch):
        _login(client, db_session, "other_partial_user")
        monkeypatch.setattr(
            generic_product_fetch,
            "_fetch_html",
            lambda url: (
                '<html><head><meta property="og:title" content="名前だけ"></head><body></body></html>',
                url,
            ),
        )

        html = client.post(
            "/scrape/other-site",
            data={"target_url": "https://shop.example.com/item/2"},
        ).get_data(as_text=True)

        assert "読み取れませんでした" in html
        assert "価格" in html

    def test_an_unreadable_page_still_lands_on_the_manual_form(self, client, db_session, monkeypatch):
        _login(client, db_session, "other_fail_user")
        monkeypatch.setattr(
            generic_product_fetch,
            "_fetch_html",
            lambda url: ("<html><head></head><body></body></html>", url),
        )

        response = client.post(
            "/scrape/other-site",
            data={"target_url": "https://shop.example.com/item/3"},
        )
        html = response.get_data(as_text=True)

        assert response.status_code == 400
        assert "商品手動追加" in html
        # The URL is carried over so it does not have to be pasted again.
        assert "https://shop.example.com/item/3" in html

    def test_an_empty_url_is_rejected_without_a_fetch(self, client, db_session, monkeypatch):
        _login(client, db_session, "other_empty_user")

        def explode(*args, **kwargs):
            raise AssertionError("should not fetch")

        monkeypatch.setattr(generic_product_fetch, "_fetch_html", explode)

        response = client.post("/scrape/other-site", data={"target_url": ""})

        assert response.status_code == 400
        assert "商品ページのURLを入力してください" in response.get_data(as_text=True)

    def test_it_needs_a_signed_in_user(self, client, db_session):
        response = client.post(
            "/scrape/other-site",
            data={"target_url": "https://shop.example.com/item/1"},
        )

        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_a_url_pointing_inside_the_network_is_refused(self, client, db_session, monkeypatch):
        _login(client, db_session, "other_ssrf_user")

        import ipaddress
        import socket as socket_module

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [
                (
                    socket_module.AF_INET,
                    socket_module.SOCK_STREAM,
                    socket_module.IPPROTO_TCP,
                    "",
                    ("169.254.169.254", port),
                )
            ]

        monkeypatch.setattr(generic_product_fetch.socket, "getaddrinfo", fake_getaddrinfo)
        assert ipaddress.ip_address("169.254.169.254").is_link_local

        response = client.post(
            "/scrape/other-site",
            data={"target_url": "https://metadata.example.com/latest"},
        )

        assert response.status_code == 400
        assert "社内ネットワーク" in response.get_data(as_text=True)


class TestPrefilledFormPostsToTheRightPlace:
    def test_the_prefilled_form_saves_to_the_manual_add_route(self, client, db_session, monkeypatch):
        """
        The reader renders the manual add page at its own URL, so an
        action-less form would post the finished product back to the reader
        and lose it.
        """
        _login(client, db_session, "other_action_user")
        monkeypatch.setattr(
            generic_product_fetch,
            "_fetch_html",
            lambda url: (JSON_LD_PAGE, url),
        )

        html = client.post(
            "/scrape/other-site",
            data={"target_url": "https://shop.example.com/item/1"},
        ).get_data(as_text=True)

        assert 'action="/products/manual-add"' in html

    def test_a_product_read_from_another_site_can_actually_be_saved(
        self, client, db_session, monkeypatch
    ):
        from models import Product

        user = _login(client, db_session, "other_save_user")
        monkeypatch.setattr(
            generic_product_fetch,
            "_fetch_html",
            lambda url: (JSON_LD_PAGE, url),
        )

        client.post(
            "/scrape/other-site",
            data={"target_url": "https://shop.example.com/item/1"},
        )
        response = client.post(
            "/products/manual-add",
            data={
                "title": "読み取れた商品",
                "cost_price": "4800",
                "source_url": "https://shop.example.com/item/1",
                "site": "other",
                "inventory_qty": "1",
                "stock_state": "on_sale",
                "publish_status": "draft",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302
        saved = db_session.query(Product).filter_by(user_id=user.id).one()
        assert saved.last_title == "読み取れた商品"
        assert saved.site == "other"
