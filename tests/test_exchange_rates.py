"""
Daily exchange rate refresh and the admin's read-only reference bar.

The admin screens are all in yen; rates exist for the customer-facing catalog.
The behaviour that matters most is that a failed refresh is survivable — the
previously stored rates must keep working.
"""
import pytest

from models import ExchangeRate, User
from services import exchange_rate_service
from services.exchange_rate_service import (
    DEFAULT_JPY_PER_USD,
    apply_safety_margin,
    get_exchange_rates,
    get_jpy_per_usd,
    refresh_exchange_rates,
)
from time_utils import utc_now


class _FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("boom")

    def json(self):
        return self._payload


def _successful_payload():
    # The API quotes "units of X per 1 JPY".
    return {
        "result": "success",
        "rates": {
            "USD": 1 / 158.42,
            "EUR": 1 / 171.05,
            "GBP": 1 / 201.33,
            "CNY": 1 / 22.14,
            "KRW": 1 / 0.115,
            "AUD": 1 / 104.0,
        },
    }


def _login(client, db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


class TestSafetyMargin:
    def test_no_margin_leaves_the_rate_alone(self):
        assert apply_safety_margin(158.42, 0) == 158.42
        assert apply_safety_margin(158.42, None) == 158.42

    def test_margin_lowers_the_rate_so_foreign_prices_go_up(self):
        assert apply_safety_margin(158.42, 2) == pytest.approx(156.42)

    def test_margin_cannot_halve_the_rate(self):
        # A mis-typed margin must not make the catalog's prices absurd.
        assert apply_safety_margin(100.0, 90) == pytest.approx(50.0)


class TestRefresh:
    def test_refresh_stores_the_supported_currencies_as_yen_per_unit(self, db_session, monkeypatch):
        monkeypatch.setattr(
            exchange_rate_service.requests,
            "get",
            lambda *args, **kwargs: _FakeResponse(_successful_payload()),
        )

        summary = refresh_exchange_rates(db_session)

        assert summary["status"] == "ok"
        assert sorted(summary["updated"]) == ["CNY", "EUR", "GBP", "KRW", "USD"]

        stored = {row.quote_currency: row.jpy_per_unit for row in db_session.query(ExchangeRate).all()}
        assert stored["USD"] == pytest.approx(158.42)
        assert stored["KRW"] == pytest.approx(0.115)
        # AUD is not offered by the catalog, so it is not stored.
        assert "AUD" not in stored

    def test_refresh_updates_rather_than_duplicates_a_currency(self, db_session, monkeypatch):
        db_session.add(
            ExchangeRate(quote_currency="USD", jpy_per_unit=100.0, fetched_at=utc_now())
        )
        db_session.commit()

        monkeypatch.setattr(
            exchange_rate_service.requests,
            "get",
            lambda *args, **kwargs: _FakeResponse(_successful_payload()),
        )
        refresh_exchange_rates(db_session)

        usd_rows = db_session.query(ExchangeRate).filter_by(quote_currency="USD").all()
        assert len(usd_rows) == 1
        assert usd_rows[0].jpy_per_unit == pytest.approx(158.42)

    def test_a_failed_fetch_keeps_the_stored_rates(self, db_session, monkeypatch):
        fetched_at = utc_now()
        db_session.add(
            ExchangeRate(quote_currency="USD", jpy_per_unit=158.42, fetched_at=fetched_at)
        )
        db_session.commit()

        def explode(*args, **kwargs):
            raise ConnectionError("network down")

        monkeypatch.setattr(exchange_rate_service.requests, "get", explode)

        summary = refresh_exchange_rates(db_session)

        assert summary["status"] == "failed"
        assert summary["updated"] == []
        assert get_jpy_per_usd(db_session) == pytest.approx(158.42)

    def test_an_api_error_response_is_treated_as_a_failure(self, db_session, monkeypatch):
        monkeypatch.setattr(
            exchange_rate_service.requests,
            "get",
            lambda *args, **kwargs: _FakeResponse({"result": "error"}),
        )

        summary = refresh_exchange_rates(db_session)

        assert summary["status"] == "failed"
        assert db_session.query(ExchangeRate).count() == 0


class TestReads:
    def test_rates_come_back_in_the_catalog_currency_order(self, db_session, monkeypatch):
        monkeypatch.setattr(
            exchange_rate_service.requests,
            "get",
            lambda *args, **kwargs: _FakeResponse(_successful_payload()),
        )
        refresh_exchange_rates(db_session)

        codes = [rate["code"] for rate in get_exchange_rates(db_session)]
        assert codes == ["USD", "EUR", "GBP", "CNY", "KRW"]

    def test_rates_carry_a_japanese_label(self, db_session, monkeypatch):
        monkeypatch.setattr(
            exchange_rate_service.requests,
            "get",
            lambda *args, **kwargs: _FakeResponse(_successful_payload()),
        )
        refresh_exchange_rates(db_session)

        labels = {rate["code"]: rate["label"] for rate in get_exchange_rates(db_session)}
        assert labels["USD"] == "米ドル"

    def test_usd_falls_back_to_the_shared_default_when_nothing_is_stored(self, db_session):
        assert get_jpy_per_usd(db_session) == DEFAULT_JPY_PER_USD


class TestAdminScreens:
    def test_the_product_list_shows_rates_as_reference_only(self, client, db_session, monkeypatch):
        _login(client, db_session, "ratebar_user")
        monkeypatch.setattr(
            exchange_rate_service.requests,
            "get",
            lambda *args, **kwargs: _FakeResponse(_successful_payload()),
        )
        refresh_exchange_rates(db_session)

        html = client.get("/").get_data(as_text=True)

        assert "参考レート" in html
        assert "米ドル" in html
        # The hand-typed rate field is gone.
        assert 'name="rate"' not in html
        assert "為替レート（1ドルあたりの円）" not in html

    def test_the_product_list_hides_the_bar_before_the_first_refresh(self, client, db_session):
        _login(client, db_session, "ratebar_empty_user")

        html = client.get("/").get_data(as_text=True)

        assert "参考レート" not in html

    def test_settings_saves_the_safety_margin(self, client, db_session):
        user = _login(client, db_session, "margin_user")

        client.post(
            "/settings/exchange-rate/margin",
            data={"exchange_rate_margin": "2"},
            follow_redirects=True,
        )

        db_session.refresh(user)
        assert user.exchange_rate_margin == 2

    def test_settings_rejects_a_negative_safety_margin(self, client, db_session):
        user = _login(client, db_session, "margin_negative_user")

        client.post(
            "/settings/exchange-rate/margin",
            data={"exchange_rate_margin": "-5"},
            follow_redirects=True,
        )

        db_session.refresh(user)
        assert user.exchange_rate_margin == 0

    def test_settings_reports_a_failed_manual_refresh_without_erroring(
        self, client, db_session, monkeypatch
    ):
        _login(client, db_session, "manual_refresh_user")

        def explode(*args, **kwargs):
            raise ConnectionError("network down")

        monkeypatch.setattr(exchange_rate_service.requests, "get", explode)

        response = client.post(
            "/settings/exchange-rate/refresh",
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert "前回のレートを使い続けます" in response.get_data(as_text=True)


class TestCatalogFallback:
    def test_catalog_rates_are_shaded_by_the_owner_margin(self, db_session, monkeypatch):
        from routes.catalog import _build_server_rates

        user = User(username="catalog_margin_user")
        user.set_password("testpassword")
        user.exchange_rate_margin = 2
        db_session.add(user)
        db_session.commit()

        monkeypatch.setattr(
            exchange_rate_service.requests,
            "get",
            lambda *args, **kwargs: _FakeResponse(_successful_payload()),
        )
        refresh_exchange_rates(db_session)

        server_rates = _build_server_rates(db_session, user.id)

        assert server_rates["JPY"] == 1
        # 1 USD is treated as 156.42 yen rather than 158.42, so a ¥15,642 item
        # prices at 100 USD instead of 98.74.
        assert server_rates["USD"] == pytest.approx(1 / 156.42)

    def test_catalog_rates_are_empty_before_the_first_refresh(self, db_session):
        from routes.catalog import _build_server_rates

        user = User(username="catalog_no_rates_user")
        user.set_password("testpassword")
        db_session.add(user)
        db_session.commit()

        assert _build_server_rates(db_session, user.id) == {}
