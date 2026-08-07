"""
The "start here" guide.

Students get stuck on the order of operations rather than on any one screen, so
the first sign-in lands here instead of on an empty product list.
"""
from models import User
from time_utils import utc_now


def _create_user(db_session, username, last_login_at=None):
    user = User(username=username, last_login_at=last_login_at)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username):
    return client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )


class TestGuidePage:
    def test_it_renders_the_five_steps_and_the_glossary(self, client, db_session):
        _create_user(db_session, "guide_user", last_login_at=utc_now())
        _login(client, "guide_user")

        html = client.get("/guide").get_data(as_text=True)

        assert "価格表ができるまで（5ステップ）" in html
        assert "2種類のリストを使い分ける" in html
        assert "画面に出てくる言葉" in html
        assert "よくあるご質問" in html

    def test_it_needs_a_signed_in_user(self, client, db_session):
        response = client.get("/guide")

        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_the_menu_links_to_it(self, client, db_session):
        _create_user(db_session, "guide_menu_user", last_login_at=utc_now())
        _login(client, "guide_menu_user")

        html = client.get("/").get_data(as_text=True)

        assert "はじめての方へ" in html


class TestFirstLoginRedirect:
    def test_the_first_sign_in_starts_on_the_guide(self, client, db_session):
        _create_user(db_session, "first_time_user")

        response = _login(client, "first_time_user")

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/guide")

    def test_later_sign_ins_go_to_the_product_list(self, client, db_session):
        _create_user(db_session, "returning_user", last_login_at=utc_now())

        response = _login(client, "returning_user")

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

    def test_the_second_sign_in_no_longer_redirects_to_the_guide(self, client, db_session):
        _create_user(db_session, "second_login_user")

        _login(client, "second_login_user")
        client.get("/logout")
        response = _login(client, "second_login_user")

        assert not response.headers["Location"].endswith("/guide")
