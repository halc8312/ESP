"""
What a failed CSRF check looks like to the operator.

Reproduced on the live site: submitting a login form that was rendered under an
earlier session returns "Bad Request / The CSRF tokens do not match." — a page
with no explanation and no way forward. The check fails for ordinary reasons
(a form left open while another tab replaced the session cookie, a page
restored from history, a token past its lifetime), so the failure has to be
recoverable.
"""
from models import User
from time_utils import utc_now


def _create_user(db_session, username):
    user = User(username=username, last_login_at=utc_now())
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _csrf_client(app):
    """A client with CSRF enforcement on, unlike the shared test client."""
    app.config["WTF_CSRF_ENABLED"] = True
    return app.test_client()


class TestLoginCsrfFailure:
    def test_a_stale_login_form_is_answered_with_the_login_page(self, app, db_session):
        _create_user(db_session, "csrf_login_user")
        client = _csrf_client(app)
        try:
            response = client.post(
                "/login",
                data={
                    "csrf_token": "a-token-from-some-earlier-session",
                    "username": "csrf_login_user",
                    "password": "testpassword",
                },
            )
            html = response.get_data(as_text=True)

            assert response.status_code == 400
            # The login form comes back, so the operator can simply try again.
            assert 'name="username"' in html
            assert "もう一度お試しください" in html
            # Not the raw framework wording.
            assert "The CSRF tokens do not match" not in html
            assert "Bad Request" not in html
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_the_returned_form_carries_a_usable_token(self, app, db_session):
        _create_user(db_session, "csrf_retry_user")
        client = _csrf_client(app)
        try:
            html = client.post(
                "/login",
                data={"csrf_token": "stale", "username": "x", "password": "y"},
            ).get_data(as_text=True)

            assert 'name="csrf_token"' in html
            token = html.split('name="csrf_token" value="')[1].split('"')[0]
            assert token
            # Retrying with it succeeds, so the page is not a dead end.
            retry = client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "username": "csrf_retry_user",
                    "password": "testpassword",
                },
            )
            assert retry.status_code == 302
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


class TestOtherPagesCsrfFailure:
    def test_other_forms_get_a_readable_page_too(self, app, db_session):
        _create_user(db_session, "csrf_other_user")
        client = _csrf_client(app)
        try:
            client.post(
                "/login",
                data={"username": "csrf_other_user", "password": "testpassword"},
            )
            response = client.post("/settings/keyword/add", data={"keyword": "テスト"})
            html = response.get_data(as_text=True)

            assert response.status_code == 400
            assert "もう一度お試しください" in html
            assert "The CSRF" not in html
        finally:
            app.config["WTF_CSRF_ENABLED"] = False
