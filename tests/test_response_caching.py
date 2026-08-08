"""
Cache headers on rendered pages.

Every page carries a per-session CSRF token. If a caching layer in front of the
app stores one, it can hand a login page — and its token — to a visitor whose
session holds a different token, which surfaces as "The CSRF tokens do not
match" on an ordinary sign-in.
"""
from models import User
from time_utils import utc_now


def _create_user(db_session, username):
    user = User(username=username, last_login_at=utc_now())
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username):
    return client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )


class TestNoStoreOnRenderedPages:
    def test_the_login_page_is_never_stored(self, client):
        response = client.get("/login")

        assert response.status_code == 200
        assert "no-store" in response.headers.get("Cache-Control", "")

    def test_the_login_page_varies_on_the_session_cookie(self, client):
        response = client.get("/login")

        assert "Cookie" in response.headers.get("Vary", "")

    def test_a_signed_in_page_is_never_stored(self, client, db_session):
        _create_user(db_session, "cache_headers_user")
        _login(client, "cache_headers_user")

        response = client.get("/")

        assert response.status_code == 200
        assert "no-store" in response.headers.get("Cache-Control", "")

    def test_the_public_catalog_is_never_stored(self, client, db_session):
        from models import PriceList

        user = _create_user(db_session, "cache_catalog_user")
        db_session.add(
            PriceList(
                user_id=user.id,
                name="Public",
                token="cache-headers-token",
                is_active=True,
            )
        )
        db_session.commit()

        response = client.get("/catalog/cache-headers-token")

        assert response.status_code == 200
        assert "no-store" in response.headers.get("Cache-Control", "")


class TestStaticFilesStayCacheable:
    def test_static_assets_keep_their_own_caching(self, client):
        response = client.get("/static/css/style.css")

        assert response.status_code == 200
        assert "no-store" not in response.headers.get("Cache-Control", "")
