import pytest

from models import PriceList, PriceListItem, Product, User


class FakeQueue:
    def __init__(self):
        self.jobs = {}

    def get_status(self, job_id, user_id=None):
        job = self.jobs.get(job_id)
        if not job:
            return None
        if user_id is not None and job['user_id'] != user_id:
            return None
        return {k: v for k, v in job.items() if k != 'user_id'}


def login_user(client, db_session, username):
    user = User(username=username)
    user.set_password('testpassword')
    db_session.add(user)
    db_session.commit()

    client.post('/login', data={
        'username': username,
        'password': 'testpassword'
    })
    return user


def make_completed_job(user_id, items=None):
    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'completed',
        'result': {
            'items': items if items is not None else [
                {
                    'url': 'https://jp.mercari.com/item/m-list-only-1',
                    'title': 'List Only Item 1',
                    'price': 1500,
                    'status': 'on_sale',
                    'description': 'list only 1',
                    'image_urls': ['https://img.example.com/l1.jpg'],
                },
                {
                    'url': 'https://jp.mercari.com/item/m-list-only-2',
                    'title': 'List Only Item 2',
                    'price': 2500,
                    'status': 'on_sale',
                    'description': 'list only 2',
                    'image_urls': ['https://img.example.com/l2.jpg'],
                },
            ],
            'site': 'mercari',
        },
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': None,
        'user_id': user_id,
    }
    return fake_queue


def test_register_to_existing_pricelist_saves_unlisted_products(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'pl_register_existing_user')
    price_list = PriceList(user_id=user.id, name='Test List', token='token-existing-1')
    db_session.add(price_list)
    db_session.commit()

    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    response = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0, 1],
        'price_list_id': price_list.id,
    })

    assert response.status_code == 200
    assert response.json['ok'] is True
    assert response.json['registered_count'] == 2
    assert response.json['added_to_list_count'] == 2
    assert response.json['price_list_id'] == price_list.id
    assert response.json['price_list_name'] == 'Test List'

    db_session.expire_all()
    products = db_session.query(Product).filter_by(user_id=user.id).all()
    assert len(products) == 2
    assert all(p.is_listed is False for p in products)

    items = db_session.query(PriceListItem).filter_by(price_list_id=price_list.id).all()
    assert {item.product_id for item in items} == {p.id for p in products}


def test_register_to_new_pricelist_creates_list(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'pl_register_new_user')
    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    response = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0],
        'new_list_name': 'Inquiry Customer List',
    })

    assert response.status_code == 200
    assert response.json['ok'] is True
    assert response.json['price_list_name'] == 'Inquiry Customer List'
    assert response.json['added_to_list_count'] == 1

    db_session.expire_all()
    price_list = db_session.query(PriceList).filter_by(user_id=user.id).one()
    assert price_list.name == 'Inquiry Customer List'
    assert price_list.token

    items = db_session.query(PriceListItem).filter_by(price_list_id=price_list.id).all()
    assert len(items) == 1


def test_register_to_pricelist_rejects_other_users_list(client, db_session, monkeypatch):
    other = User(username='pl_other_owner')
    other.set_password('testpassword')
    db_session.add(other)
    db_session.commit()
    other_list = PriceList(user_id=other.id, name='Other List', token='token-other-1')
    db_session.add(other_list)
    db_session.commit()

    user = login_user(client, db_session, 'pl_register_isolation_user')
    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    response = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0],
        'price_list_id': other_list.id,
    })

    assert response.status_code == 404
    assert db_session.query(PriceListItem).filter_by(price_list_id=other_list.id).count() == 0
    # 検証はリスト保存前に行われるため、不可視な孤児商品は作られない
    assert db_session.query(Product).filter_by(user_id=user.id).count() == 0


def test_register_to_pricelist_requires_list_target(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'pl_register_no_target_user')
    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    response = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0],
    })

    assert response.status_code == 400


def test_register_to_pricelist_skips_duplicate_list_items(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'pl_register_dedupe_user')
    price_list = PriceList(user_id=user.id, name='Dedupe List', token='token-dedupe-1')
    db_session.add(price_list)
    db_session.commit()

    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    first = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0, 1],
        'price_list_id': price_list.id,
    })
    assert first.status_code == 200

    second = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0, 1],
        'price_list_id': price_list.id,
    })
    assert second.status_code == 200
    assert second.json['added_to_list_count'] == 0

    db_session.expire_all()
    assert db_session.query(PriceListItem).filter_by(price_list_id=price_list.id).count() == 2


def test_unlisted_products_hidden_from_index_but_visible_in_catalog(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'pl_visibility_user')
    price_list = PriceList(user_id=user.id, name='Visibility List', token='token-visibility-1')
    db_session.add(price_list)
    db_session.commit()

    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    response = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0],
        'price_list_id': price_list.id,
    })
    assert response.status_code == 200

    index_response = client.get('/')
    assert index_response.status_code == 200
    assert 'List Only Item 1' not in index_response.get_data(as_text=True)

    catalog_response = client.get(f'/catalog/{price_list.token}')
    assert catalog_response.status_code == 200
    catalog_html = catalog_response.get_data(as_text=True)
    assert 'List Only Item 1' in catalog_html
    assert 'jp.mercari.com' not in catalog_html


def test_register_selected_keeps_products_listed(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'pl_listed_default_user')
    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    response = client.post('/scrape/register-selected', json={
        'job_id': 'job-1',
        'selected_indices': [0],
    })

    assert response.status_code == 200
    db_session.expire_all()
    product = db_session.query(Product).filter_by(user_id=user.id).one()
    assert product.is_listed is True


def test_register_selected_relists_previously_unlisted_product(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'pl_relist_user')
    price_list = PriceList(user_id=user.id, name='Relist List', token='token-relist-1')
    db_session.add(price_list)
    db_session.commit()

    monkeypatch.setattr('routes.scrape.get_queue', lambda: make_completed_job(user.id))

    first = client.post('/scrape/register-to-pricelist', json={
        'job_id': 'job-1',
        'selected_indices': [0],
        'price_list_id': price_list.id,
    })
    assert first.status_code == 200

    second = client.post('/scrape/register-selected', json={
        'job_id': 'job-1',
        'selected_indices': [0],
    })
    assert second.status_code == 200

    db_session.expire_all()
    product = db_session.query(Product).filter_by(user_id=user.id).one()
    assert product.is_listed is True
