import pytest

from models import Product, User


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.counter = 0

    def enqueue(self, site, task_fn, task_args=(), task_kwargs=None, user_id=None):
        self.counter += 1
        job_id = f'job-{self.counter}'
        result = task_fn(*(task_args or ()), **(task_kwargs or {}))
        self.jobs[job_id] = {
            'job_id': job_id,
            'status': 'completed',
            'result': result,
            'error': None,
            'elapsed_seconds': 0.1,
            'queue_position': None,
            'user_id': user_id,
        }
        return job_id

    def get_status(self, job_id, user_id=None):
        job = self.jobs.get(job_id)
        if not job:
            return None
        if user_id is not None and job['user_id'] != user_id:
            return None
        return {k: v for k, v in job.items() if k != 'user_id'}


def login_user(client, db_session, username='preview_user'):
    user = User(username=username)
    user.set_password('testpassword')
    db_session.add(user)
    db_session.commit()

    client.post('/login', data={
        'username': username,
        'password': 'testpassword'
    })
    return user


def test_scrape_run_preview_returns_json_without_saving(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'preview_json_user')
    fake_queue = FakeQueue()
    build_calls = {}

    def fake_build_scrape_task(**kwargs):
        build_calls['persist_to_db'] = kwargs['persist_to_db']
        return lambda: {
            'items': [
                {
                    'url': 'https://jp.mercari.com/item/m-preview-1',
                    'title': 'Preview Item 1',
                    'price': 1200,
                    'status': 'on_sale',
                    'description': 'preview',
                    'image_urls': ['https://img.example.com/1.jpg'],
                }
            ],
            'new_count': 0,
            'updated_count': 0,
            'excluded_count': 0,
            'site': 'mercari',
            'persist_to_db': False,
        }

    monkeypatch.setattr('routes.scrape.get_queue', lambda: fake_queue)
    monkeypatch.setattr('routes.scrape._build_scrape_task', fake_build_scrape_task)

    response = client.post('/scrape/run', data={
        'site': 'mercari',
        'keyword': 'preview',
        'response_mode': 'preview'
    })

    assert response.status_code == 202
    assert response.json['job_id'] == 'job-1'
    assert build_calls['persist_to_db'] is False
    assert db_session.query(Product).filter_by(user_id=user.id).count() == 0


def test_scrape_run_standard_mode_redirects_and_keeps_persist_flow(client, db_session, monkeypatch):
    login_user(client, db_session, 'preview_redirect_user')
    fake_queue = FakeQueue()
    build_calls = {}

    def fake_build_scrape_task(**kwargs):
        build_calls['persist_to_db'] = kwargs['persist_to_db']
        return lambda: {'items': [], 'site': 'mercari', 'persist_to_db': True}

    monkeypatch.setattr('routes.scrape.get_queue', lambda: fake_queue)
    monkeypatch.setattr('routes.scrape._build_scrape_task', fake_build_scrape_task)

    response = client.post('/scrape/run', data={
        'site': 'mercari',
        'keyword': 'standard'
    })

    assert response.status_code == 302
    assert '/scrape/status/job-1' in response.headers['Location']
    assert build_calls['persist_to_db'] is True


def test_register_selected_saves_only_selected_items(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'register_selected_user')
    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'completed',
        'result': {
            'items': [
                {
                    'url': 'https://jp.mercari.com/item/m-preview-1',
                    'title': 'Preview Item 1',
                    'price': 1200,
                    'status': 'on_sale',
                    'description': 'preview 1',
                    'image_urls': ['https://img.example.com/1.jpg'],
                },
                {
                    'url': 'https://jp.mercari.com/item/m-preview-2',
                    'title': 'Preview Item 2',
                    'price': 2400,
                    'status': 'on_sale',
                    'description': 'preview 2',
                    'image_urls': ['https://img.example.com/2.jpg'],
                },
            ],
            'site': 'mercari',
        },
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': None,
        'user_id': user.id,
    }

    monkeypatch.setattr('routes.scrape.get_queue', lambda: fake_queue)

    response = client.post('/scrape/register-selected', json={
        'job_id': 'job-1',
        'selected_indices': [1]
    })

    assert response.status_code == 200
    assert response.json['ok'] is True
    assert response.json['registered_count'] == 1

    products = db_session.query(Product).filter_by(user_id=user.id).all()
    assert len(products) == 1
    assert products[0].last_title == 'Preview Item 2'


def test_scrape_status_hides_other_users_job(client, db_session, monkeypatch):
    login_user(client, db_session, 'status_owner_user')
    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'completed',
        'result': {'items': [], 'site': 'mercari'},
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': None,
        'user_id': 9999,
    }

    monkeypatch.setattr('routes.api.get_queue', lambda: fake_queue)

    response = client.get('/api/scrape/status/job-1')
    assert response.status_code == 404
    assert response.json['error'] == 'Job not found'
