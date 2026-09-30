from datetime import timedelta

import pytest

from models import PriceList, PriceListItem, Product, User, Shop
from services.monitor_service import MonitorService
from services.patrol.base_patrol import PatrolResult
from services.scrape_job_store import ScrapeQueueFull, create_job_record, mark_job_completed
from time_utils import utc_now


def _user(session, name):
    user = User(username=name, email=f"{name}@example.test", password_hash="test")
    session.add(user)
    session.flush()
    return user


def test_admission_owner_limit_is_released_only_on_completion(client, db_session, monkeypatch):
    owner = _user(db_session, "admission_owner")
    other = _user(db_session, "admission_other")
    db_session.commit()
    monkeypatch.setenv("SCRAPE_MAX_ACTIVE_JOBS_PER_USER", "2")
    create_job_record("admit-1", "recordcity", user_id=owner.id)
    create_job_record("admit-2", "recordcity", user_id=owner.id)
    with pytest.raises(ScrapeQueueFull):
        create_job_record("admit-rejected", "recordcity", user_id=owner.id)
    create_job_record("admit-other", "recordcity", user_id=other.id)
    mark_job_completed("admit-1", {"items": []})
    create_job_record("admit-3", "recordcity", user_id=owner.id)


def test_visible_owned_catalog_only_products_are_patrolled(client, db_session, monkeypatch):
    owner = _user(db_session, "patrol_catalog_owner")
    other = _user(db_session, "patrol_catalog_other")
    now = utc_now()
    products = []
    for index in range(4):
        product = Product(user_id=owner.id, site="mercari", source_url=f"https://jp.mercari.com/item/m{3000+index}",
            last_price=1000, last_title="record", last_status="on_sale", is_listed=False,
            created_at=now-timedelta(days=1), updated_at=now-timedelta(days=1),
            detail_fetch_state="pending" if index == 3 else None)
        db_session.add(product)
        db_session.flush()
        pricelist = PriceList(user_id=other.id if index == 2 else owner.id,
            token=f"patrol-catalog-{index}", name="catalog", is_active=True)
        db_session.add(pricelist)
        db_session.flush()
        db_session.add(PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=index != 1))
        products.append(product)
    db_session.commit()
    calls = []

    class Patrol:
        def fetch(self, url, driver=None):
            calls.append(url)
            return PatrolResult(price=1900, status="on_sale", variants=[])

    monkeypatch.setattr(MonitorService, "_patrols", {"mercari": Patrol()})
    summary = MonitorService.check_stale_products(limit=20)
    assert summary["status"] == "completed"
    assert summary["selected_count"] == 1
    assert calls == [products[0].source_url]


def test_expired_catalog_only_product_is_not_patrolled(client, db_session, monkeypatch):
    owner = _user(db_session, "patrol_expired_owner")
    product = Product(user_id=owner.id, site="mercari", source_url="https://jp.mercari.com/item/m9001", is_listed=False)
    pricelist = PriceList(user_id=owner.id, token="patrol-expired", name="expired", is_active=True,
        unpublish_at=utc_now()-timedelta(seconds=1))
    db_session.add_all([product, pricelist])
    db_session.flush()
    db_session.add(PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True))
    db_session.commit()
    monkeypatch.setattr(MonitorService, "_patrols", {"mercari": object()})
    summary = MonitorService.check_stale_products(limit=20)
    assert summary["selected_count"] == 0


@pytest.mark.parametrize("foreign_product_shop,foreign_list_shop,expected", [
    (False, False, 1), (True, False, 0), (False, True, 0), (True, True, 0),
])
def test_catalog_patrol_allows_owned_cross_shop_membership_only(
        client, db_session, monkeypatch, foreign_product_shop, foreign_list_shop, expected):
    owner = _user(db_session, "cross_shop_owner")
    other = _user(db_session, "cross_shop_other")
    product_shop = Shop(user_id=other.id if foreign_product_shop else owner.id, name="product shop")
    list_shop = Shop(user_id=other.id if foreign_list_shop else owner.id, name="catalog shop")
    db_session.add_all([product_shop, list_shop])
    db_session.flush()
    product = Product(user_id=owner.id, shop_id=product_shop.id, site="mercari",
        source_url="https://jp.mercari.com/item/m9551", is_listed=False,
        last_price=1000, last_status="on_sale", last_title="record")
    pricelist = PriceList(user_id=owner.id, shop_id=list_shop.id, token="cross-shop-catalog",
        name="catalog", is_active=True)
    db_session.add_all([product, pricelist])
    db_session.flush()
    db_session.add(PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True))
    db_session.commit()
    calls = []

    class Patrol:
        def fetch(self, url):
            calls.append(url)
            return PatrolResult(price=1900, status="on_sale", variants=[])

    monkeypatch.setattr(MonitorService, "_patrols", {"mercari": Patrol()})
    summary = MonitorService.check_stale_products(limit=20)
    assert summary["selected_count"] == expected
    assert calls == ([product.source_url] if expected else [])
