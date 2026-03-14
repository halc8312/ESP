from datetime import datetime, timedelta

from models import Product, User
from services.monitor_service import MonitorService
from services.patrol.base_patrol import PatrolResult
from utils import is_valid_detail_url


class FakePatrol:
    def __init__(self, result):
        self.result = result
        self.called_urls = []

    def fetch(self, url, driver=None):
        self.called_urls.append(url)
        return self.result



def _create_user(db_session, username):
    user = User(username=username)
    user.set_password('testpassword')
    db_session.add(user)
    db_session.commit()
    return user



def test_monitor_service_skips_deleted_products(client, db_session, monkeypatch):
    user = _create_user(db_session, 'monitor_deleted_user')
    old_time = datetime.utcnow() - timedelta(days=1)

    active_product = Product(
        user_id=user.id,
        site='mercari',
        source_url='https://jp.mercari.com/item/m-active-monitor',
        last_title='Active Item',
        last_price=1000,
        last_status='on_sale',
        archived=False,
        deleted_at=None,
        created_at=old_time,
        updated_at=old_time,
    )
    deleted_product = Product(
        user_id=user.id,
        site='mercari',
        source_url='https://jp.mercari.com/item/m-deleted-monitor',
        last_title='Deleted Item',
        last_price=900,
        last_status='on_sale',
        archived=False,
        deleted_at=datetime.utcnow(),
        created_at=old_time,
        updated_at=old_time - timedelta(minutes=5),
    )
    db_session.add_all([active_product, deleted_product])
    db_session.commit()

    fake_patrol = FakePatrol(PatrolResult(price=1500, status='sold', variants=[]))
    monkeypatch.setattr(MonitorService, '_patrols', {'mercari': fake_patrol})

    MonitorService.check_stale_products(limit=10)

    db_session.expire_all()
    refreshed_active = db_session.query(Product).filter_by(id=active_product.id).one()
    refreshed_deleted = db_session.query(Product).filter_by(id=deleted_product.id).one()

    assert fake_patrol.called_urls == ['https://jp.mercari.com/item/m-active-monitor']
    assert refreshed_active.last_price == 1500
    assert refreshed_active.last_status == 'sold'
    assert refreshed_deleted.last_price == 900
    assert refreshed_deleted.last_status == 'on_sale'


# ---------------------------------------------------------------------------
# New tests for URL validation & failure backoff
# ---------------------------------------------------------------------------


def test_invalid_url_skipped_with_backoff(client, db_session, monkeypatch):
    """Products with search URLs are skipped and get backoff applied."""
    user = _create_user(db_session, 'monitor_invalid_url_user')
    old_time = datetime.utcnow() - timedelta(days=1)

    # A Yahoo product with a search URL (not a detail page)
    bad_product = Product(
        user_id=user.id,
        site='yahoo',
        source_url='https://shopping.yahoo.co.jp/search?p=shoes',
        last_title='Search Result',
        last_price=500,
        last_status='active',
        archived=False,
        deleted_at=None,
        patrol_fail_count=0,
        created_at=old_time,
        updated_at=old_time,
    )
    db_session.add(bad_product)
    db_session.commit()

    fake_patrol = FakePatrol(PatrolResult(price=999, status='active'))
    monkeypatch.setattr(MonitorService, '_patrols', {'yahoo': fake_patrol})

    MonitorService.check_stale_products(limit=10)

    db_session.expire_all()
    refreshed = db_session.query(Product).filter_by(id=bad_product.id).one()

    # Patrol should NOT have been called
    assert fake_patrol.called_urls == []
    # Fail count incremented
    assert refreshed.patrol_fail_count == 1
    # updated_at pushed into the future (at least 10 min from now)
    assert refreshed.updated_at > datetime.utcnow() + timedelta(minutes=10)
    # Price unchanged
    assert refreshed.last_price == 500


def test_patrol_failure_updates_timestamp(client, db_session, monkeypatch):
    """When patrol.fetch returns an error, backoff is applied."""
    user = _create_user(db_session, 'monitor_fail_ts_user')
    old_time = datetime.utcnow() - timedelta(days=1)

    product = Product(
        user_id=user.id,
        site='mercari',
        source_url='https://jp.mercari.com/item/m-fail-test',
        last_title='Failing Item',
        last_price=2000,
        last_status='active',
        archived=False,
        deleted_at=None,
        patrol_fail_count=2,  # Already failed twice
        created_at=old_time,
        updated_at=old_time,
    )
    db_session.add(product)
    db_session.commit()

    failing_patrol = FakePatrol(PatrolResult(error="Timeout"))
    monkeypatch.setattr(MonitorService, '_patrols', {'mercari': failing_patrol})

    MonitorService.check_stale_products(limit=10)

    db_session.expire_all()
    refreshed = db_session.query(Product).filter_by(id=product.id).one()

    # Fail count incremented from 2 → 3
    assert refreshed.patrol_fail_count == 3
    # updated_at pushed into the future (3 * 15 = 45 min backoff)
    assert refreshed.updated_at > datetime.utcnow() + timedelta(minutes=40)
    # Price NOT changed
    assert refreshed.last_price == 2000


def test_patrol_success_resets_fail_count(client, db_session, monkeypatch):
    """Successful patrol resets patrol_fail_count to 0."""
    user = _create_user(db_session, 'monitor_success_reset_user')
    old_time = datetime.utcnow() - timedelta(days=1)

    product = Product(
        user_id=user.id,
        site='mercari',
        source_url='https://jp.mercari.com/item/m-success-reset',
        last_title='Recovery Item',
        last_price=3000,
        last_status='active',
        archived=False,
        deleted_at=None,
        patrol_fail_count=5,  # Previously had 5 consecutive failures
        created_at=old_time,
        updated_at=old_time,
    )
    db_session.add(product)
    db_session.commit()

    ok_patrol = FakePatrol(PatrolResult(price=3500, status='active', variants=[]))
    monkeypatch.setattr(MonitorService, '_patrols', {'mercari': ok_patrol})

    MonitorService.check_stale_products(limit=10)

    db_session.expire_all()
    refreshed = db_session.query(Product).filter_by(id=product.id).one()

    # Fail count reset
    assert refreshed.patrol_fail_count == 0
    # Price updated
    assert refreshed.last_price == 3500
    # updated_at should be recent (not in the future)
    assert refreshed.updated_at <= datetime.utcnow() + timedelta(seconds=10)


# ---------------------------------------------------------------------------
# Unit tests for is_valid_detail_url
# ---------------------------------------------------------------------------


def test_is_valid_detail_url_valid_cases():
    """Valid detail URLs pass validation."""
    assert is_valid_detail_url("https://jp.mercari.com/item/m12345abc", "mercari")
    assert is_valid_detail_url(
        "https://store.shopping.yahoo.co.jp/example-store/item123.html", "yahoo"
    )
    assert is_valid_detail_url("https://fril.jp/product/12345", "rakuma")
    assert is_valid_detail_url(
        "https://www.suruga-ya.jp/product/detail/123456789", "surugaya"
    )
    assert is_valid_detail_url(
        "https://page.auctions.yahoo.co.jp/jp/auction/x123", "yahuoku"
    )
    assert is_valid_detail_url("https://snkrdunk.com/products/air-jordan-1", "snkrdunk")
    assert is_valid_detail_url(
        "https://offmall.hardoff.co.jp/categories/sneakers/item123", "offmall"
    )


def test_is_valid_detail_url_invalid_cases():
    """Search URLs and malformed URLs are rejected."""
    # Search URLs
    assert not is_valid_detail_url(
        "https://shopping.yahoo.co.jp/search?p=shoes", "yahoo"
    )
    assert not is_valid_detail_url(
        "https://jp.mercari.com/search?keyword=bag", "mercari"
    )
    # Empty / whitespace
    assert not is_valid_detail_url("", "mercari")
    assert not is_valid_detail_url("   ", "yahoo")
    # Wrong domain for site
    assert not is_valid_detail_url("https://google.com/something", "yahoo")
    assert not is_valid_detail_url("https://fril.jp/listing/popular", "rakuma")


def test_is_valid_detail_url_unknown_site():
    """Unknown sites are allowed through (don't break future additions)."""
    assert is_valid_detail_url("https://newsite.com/item/123", "newsite")
