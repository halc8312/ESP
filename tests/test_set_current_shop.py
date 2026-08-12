"""
Choosing the shop in the header selector.

Reported from the live site: picking a shop and pressing the confirm button
returned "500 サーバー内部エラーが発生しました". The route passed the form value
— text — straight into a filter on an integer column. SQLite compares those
happily, PostgreSQL refuses:

    operator does not exist: integer = character varying

so the whole test suite passed while production failed on every selection.
These tests pin the coercion, since the type mismatch itself is invisible on
SQLite.
"""
import pytest

from models import Shop, User
from time_utils import utc_now


def _create_user(db_session, username):
    user = User(username=username, last_login_at=utc_now())
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, db_session, username):
    user = _create_user(db_session, username)
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


def _create_shop(db_session, user, name):
    shop = Shop(user_id=user.id, name=name)
    db_session.add(shop)
    db_session.commit()
    return shop


class TestChoosingAShop:
    def test_the_selection_is_stored_as_a_number(self, client, db_session):
        user = _login(client, db_session, "shop_select_user")
        shop = _create_shop(db_session, user, "My Shop")

        response = client.post("/set_current_shop", data={"shop_id": str(shop.id)})

        assert response.status_code == 302
        with client.session_transaction() as flask_session:
            stored = flask_session["current_shop_id"]
        # Not the string the form sent: the database is asked with an integer.
        assert stored == shop.id
        assert isinstance(stored, int)

    def test_clearing_the_selection_works(self, client, db_session):
        user = _login(client, db_session, "shop_clear_user")
        shop = _create_shop(db_session, user, "My Shop")
        client.post("/set_current_shop", data={"shop_id": str(shop.id)})

        response = client.post("/set_current_shop", data={"shop_id": ""})

        assert response.status_code == 302
        with client.session_transaction() as flask_session:
            assert "current_shop_id" not in flask_session

    def test_another_accounts_shop_is_not_selected(self, client, db_session):
        stranger = _create_user(db_session, "shop_stranger")
        stranger_shop = _create_shop(db_session, stranger, "Not Mine")
        _login(client, db_session, "shop_isolation_user")

        response = client.post("/set_current_shop", data={"shop_id": str(stranger_shop.id)})

        assert response.status_code == 302
        with client.session_transaction() as flask_session:
            assert "current_shop_id" not in flask_session


class TestValuesThatAreNotAnId:
    @pytest.mark.parametrize("raw", ["abc", "1; DROP TABLE shops", "1.5", "12abc", "None"])
    def test_the_database_is_never_asked(self, client, db_session, monkeypatch, raw):
        user = _login(client, db_session, f"shop_bad_input_{abs(hash(raw))}")
        shop = _create_shop(db_session, user, "My Shop")
        client.post("/set_current_shop", data={"shop_id": str(shop.id)})

        def _explode():
            raise AssertionError("queried the database with a value that is not an id")

        monkeypatch.setattr("routes.shops.SessionLocal", _explode)

        response = client.post("/set_current_shop", data={"shop_id": raw})

        assert response.status_code == 302
        # The choice already made is left alone rather than cleared.
        with client.session_transaction() as flask_session:
            assert flask_session["current_shop_id"] == shop.id
