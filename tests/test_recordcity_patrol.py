"""RecordCity patrol checks use sanitized detail JSON-LD fixtures, no network."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

import recordcity_db
from models import Product, User, Variant
from services.html_page_adapter import HtmlPageAdapter
from services.monitor_service import MonitorService
from services.patrol.recordcity_patrol import RecordCityPatrol
from services.scrape_safety import ScrapeBlockedError, ScrapeHttpError
from time_utils import utc_now


URL = "https://www.recordcity.jp/ja/catalog/4936480"
DETAIL = {
    "@context": "https://schema.org", "@type": "Product", "sku": 4936480,
    "name": "Sunrise / Son of Pin Head",
    "offers": {"@type": "Offer", "price": 2420, "priceCurrency": "JPY",
               "availability": "https://schema.org/InStock"},
}


def page(product=None):
    return HtmlPageAdapter('<script type="application/ld+json">'
        + json.dumps(DETAIL if product is None else product) + "</script>", url=URL)


@pytest.fixture(autouse=True)
def no_network_or_alerts(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected live fetch")
    monkeypatch.setattr(recordcity_db, "_fetch_page", forbidden)
    monkeypatch.setattr("services.patrol.base_patrol.report_patrol_result", lambda *args, **kwargs: False)
    monkeypatch.setattr("services.monitor_service.record_observation_safely", lambda **kwargs: True)


def fetch_fixture(monkeypatch, product=None, failure=None):
    calls = []
    def fetch(url, kind):
        calls.append((url, kind))
        if failure is not None:
            raise failure
        return page(product)
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    return calls


def test_recordcity_patrol_is_registered():
    assert isinstance(MonitorService._patrols["recordcity"], RecordCityPatrol)


@pytest.mark.parametrize("availability,status,stock", [
    ("InStock", "active", 1), ("LimitedAvailability", "active", 1),
    ("PreOrder", "active", 1), ("BackOrder", "active", 1),
    ("OutOfStock", "sold", 0), ("Discontinued", "sold", 0),
])
def test_patrol_reads_one_target_detail_and_maps_verified_availability(monkeypatch, availability, status, stock):
    product = deepcopy(DETAIL)
    product["offers"]["availability"] = f"https://schema.org/{availability}"
    calls = fetch_fixture(monkeypatch, product)
    result = RecordCityPatrol().fetch(URL)
    assert calls == [(URL, "detail")]
    assert result.success and result.status == status and result.price == 2420
    assert result.confidence == "high" and result.evidence_strength == "hard"
    assert result.variants == [{"name": "Default Title", "stock": stock, "price": 2420}]


def test_patrol_matches_requested_product_among_multiple_nodes(monkeypatch):
    other = deepcopy(DETAIL)
    other["sku"] = 9999
    other["offers"]["price"] = 1
    fetch_fixture(monkeypatch, [other, DETAIL])
    assert RecordCityPatrol().fetch(URL).price == 2420


@pytest.mark.parametrize("change", [
    {"sku": 9999}, {"sku": None}, {"name": ""}, {"name": {"invalid": "name"}}, {"offers": []},
    {"offers": [{"price": 1}, {"price": 2}]},
])
def test_unverified_product_or_offer_is_not_a_success(monkeypatch, change):
    product = {**deepcopy(DETAIL), **change}
    fetch_fixture(monkeypatch, product)
    result = RecordCityPatrol().fetch(URL)
    assert not result.success and result.price is None and result.status == "unknown"


@pytest.mark.parametrize("offer_change", [
    {"availability": "unknown"}, {"availability": None}, {"price": None},
    {"priceCurrency": "USD"}, {"priceCurrency": None}, {"price": "from 2420"},
    {"price": -2420}, {"price": 0}, {"price": True}, {"price": "2,42"},
    {"price": 2420.5}, {"price": 2147483648}, {"@type": "AggregateOffer"},
])
def test_uncertain_active_price_or_stock_never_persists_as_success(monkeypatch, offer_change):
    product = deepcopy(DETAIL)
    product["offers"].update(offer_change)
    fetch_fixture(monkeypatch, product)
    result = RecordCityPatrol().fetch(URL)
    assert not result.success and result.price is None and result.confidence == "low"


def test_explicit_sold_without_price_can_update_stock_only(monkeypatch):
    product = deepcopy(DETAIL)
    product["offers"] = {"availability": "https://schema.org/OutOfStock"}
    fetch_fixture(monkeypatch, product)
    result = RecordCityPatrol().fetch(URL)
    assert result.success and result.status == "sold" and result.price is None


@pytest.mark.parametrize("status,error_class", [(403, ScrapeBlockedError), (429, ScrapeBlockedError), (429, ScrapeHttpError)])
def test_access_block_stops_after_one_fetch(monkeypatch, status, error_class):
    calls = fetch_fixture(monkeypatch, failure=error_class("access blocked", status_code=status))
    result = RecordCityPatrol().fetch(URL)
    assert calls == [(URL, "detail")]
    assert not result.success and result.status == "blocked" and result.price is None
    assert result.error == f"RecordCity access blocked (HTTP {status})"


@pytest.mark.parametrize("status", [404, 410])
def test_definitive_missing_detail_is_deleted_without_retry(monkeypatch, status):
    calls = fetch_fixture(monkeypatch, failure=ScrapeHttpError("missing", status_code=status))
    result = RecordCityPatrol().fetch(URL)
    assert calls == [(URL, "detail")]
    assert result.success and result.status == "deleted" and result.price is None


def test_invalid_url_never_fetches(monkeypatch):
    calls = fetch_fixture(monkeypatch)
    result = RecordCityPatrol().fetch("https://www.recordcity.jp/ja/catalog?keyword=test")
    assert calls == [] and not result.success


def create_product(db_session):
    user = User(username="recordcity-patrol-owner")
    user.set_password("testing-password")
    db_session.add(user)
    db_session.flush()
    product = Product(user_id=user.id, site="recordcity", source_url=URL,
        last_title="Keep title", last_price=6000, last_status="on_sale",
        archived=False, is_listed=True, updated_at=utc_now() - timedelta(days=2))
    db_session.add(product)
    db_session.flush()
    db_session.add(Variant(product_id=product.id, option1_value="Default Title", inventory_qty=3, price=6000))
    db_session.commit()
    return product


@pytest.mark.parametrize("failure", ["unknown", "blocked"])
def test_monitor_keeps_last_verified_price_stock_and_applies_backoff(client, db_session, monkeypatch, failure):
    product = create_product(db_session)
    if failure == "blocked":
        calls = fetch_fixture(monkeypatch, failure=ScrapeBlockedError("blocked", status_code=429))
    else:
        data = deepcopy(DETAIL)
        data["offers"]["availability"] = "unknown"
        calls = fetch_fixture(monkeypatch, data)
    monkeypatch.setattr(MonitorService, "_patrols", {"recordcity": RecordCityPatrol()})
    result = MonitorService.check_stale_products(limit=1)
    db_session.expire_all()
    saved = db_session.get(Product, product.id)
    assert result["error_count"] == 1 and calls == [(URL, "detail")]
    assert saved.last_price == 6000 and saved.last_status == "on_sale"
    assert saved.variants[0].price == 6000 and saved.variants[0].inventory_qty == 3
    assert saved.patrol_fail_count == 1 and saved.next_patrol_at > utc_now()


def test_monitor_updates_verified_price_and_stock_only(client, db_session, monkeypatch):
    product = create_product(db_session)
    fetch_fixture(monkeypatch)
    monkeypatch.setattr(MonitorService, "_patrols", {"recordcity": RecordCityPatrol()})
    result = MonitorService.check_stale_products(limit=1)
    db_session.expire_all()
    saved = db_session.get(Product, product.id)
    assert result["successful_count"] == 1
    assert saved.last_price == 2420 and saved.last_status == "on_sale"
    assert saved.last_title == "Keep title" and saved.snapshots == []
