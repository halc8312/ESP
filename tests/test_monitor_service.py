from datetime import datetime, timedelta

from models import Product, User
from services.monitor_service import MonitorService
from services.patrol.base_patrol import PatrolResult


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

