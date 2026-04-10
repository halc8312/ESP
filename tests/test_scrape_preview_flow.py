import re

import pytest

from models import Product, ScrapeJob, Shop, User
from time_utils import utc_now


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.counter = 0
        self.last_enqueue = None

    def enqueue(
        self,
        site,
        task_fn,
        task_args=(),
        task_kwargs=None,
        user_id=None,
        context=None,
        request_payload=None,
        mode=None,
    ):
        self.counter += 1
        job_id = f'job-{self.counter}'
        result = task_fn(*(task_args or ()), **(task_kwargs or {}))
        self.last_enqueue = {
            'site': site,
            'user_id': user_id,
            'context': context or {},
            'request_payload': request_payload or {},
            'mode': mode,
        }
        self.jobs[job_id] = {
            'job_id': job_id,
            'status': 'completed',
            'site': site,
            'result': result,
            'error': None,
            'elapsed_seconds': 0.1,
            'queue_position': None,
            'context': context or {},
            'created_at': 1.0,
            'finished_at': 2.0,
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

    def get_jobs_for_user(self, user_id, limit=5, include_terminal=True):
        jobs = [
            {k: v for k, v in job.items() if k != 'user_id'}
            for job in self.jobs.values()
            if job['user_id'] == user_id
        ]
        jobs.sort(key=lambda job: job['created_at'], reverse=True)
        return jobs[:limit]


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


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match is not None
    return match.group(1)


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
    assert response.json['context']['persist_to_db'] is False
    assert response.json['result_url'] == '/scrape?job_id=job-1'
    assert build_calls['persist_to_db'] is False
    assert fake_queue.last_enqueue['context']['persist_to_db'] is False
    assert fake_queue.last_enqueue['request_payload']['persist_to_db'] is False
    assert fake_queue.last_enqueue['mode'] == 'preview'
    assert db_session.query(Product).filter_by(user_id=user.id).count() == 0


def test_scrape_run_passes_current_shop_id_to_task(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'preview_shop_user')
    shop = Shop(name='Preview Shop', user_id=user.id)
    db_session.add(shop)
    db_session.commit()

    with client.session_transaction() as flask_session:
        flask_session['current_shop_id'] = shop.id

    fake_queue = FakeQueue()
    build_calls = {}

    def fake_build_scrape_task(**kwargs):
        build_calls['persist_to_db'] = kwargs['persist_to_db']
        build_calls['shop_id'] = kwargs['shop_id']
        return lambda: {'items': [], 'site': 'mercari', 'persist_to_db': True, 'shop_id': kwargs['shop_id']}

    monkeypatch.setattr('routes.scrape.get_queue', lambda: fake_queue)
    monkeypatch.setattr('routes.scrape._build_scrape_task', fake_build_scrape_task)

    response = client.post('/scrape/run', data={
        'site': 'mercari',
        'keyword': 'shop-aware'
    })

    assert response.status_code == 302
    assert build_calls['persist_to_db'] is True
    assert build_calls['shop_id'] == shop.id
    assert fake_queue.last_enqueue['request_payload']['shop_id'] == shop.id
    assert fake_queue.last_enqueue['mode'] == 'persist'



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

    db_session.expire_all()
    products = db_session.query(Product).filter_by(user_id=user.id).all()
    assert len(products) == 1
    assert products[0].last_title == 'Preview Item 2'


def test_register_selected_uses_job_shop_id_over_current_session_shop(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'register_selected_shop_user')
    source_shop = Shop(name='Job Shop', user_id=user.id)
    current_shop = Shop(name='Session Shop', user_id=user.id)
    db_session.add_all([source_shop, current_shop])
    db_session.commit()

    with client.session_transaction() as flask_session:
        flask_session['current_shop_id'] = current_shop.id

    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'completed',
        'result': {
            'items': [
                {
                    'url': 'https://jp.mercari.com/item/m-preview-shop',
                    'title': 'Preview Shop Item',
                    'price': 3300,
                    'status': 'on_sale',
                    'description': 'preview shop',
                    'image_urls': ['https://img.example.com/shop.jpg'],
                },
            ],
            'site': 'mercari',
            'shop_id': source_shop.id,
        },
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': None,
        'user_id': user.id,
    }

    monkeypatch.setattr('routes.scrape.get_queue', lambda: fake_queue)

    response = client.post('/scrape/register-selected', json={
        'job_id': 'job-1',
        'selected_indices': [0]
    })

    assert response.status_code == 200
    db_session.expire_all()
    product = db_session.query(Product).filter_by(user_id=user.id).one()
    assert product.shop_id == source_shop.id


def test_register_selected_requires_csrf_header_when_enabled(app, client, db_session, monkeypatch):
    user = login_user(client, db_session, 'register_selected_csrf_user')
    app.config['WTF_CSRF_ENABLED'] = True

    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'completed',
        'result': {
            'items': [
                {
                    'url': 'https://jp.mercari.com/item/m-preview-csrf',
                    'title': 'Preview CSRF Item',
                    'price': 1800,
                    'status': 'on_sale',
                    'description': 'preview csrf',
                    'image_urls': ['https://img.example.com/csrf.jpg'],
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

    page_response = client.get('/scrape')
    assert page_response.status_code == 200
    csrf_token = _extract_csrf_token(page_response.get_data(as_text=True))

    missing_header_response = client.post('/scrape/register-selected', json={
        'job_id': 'job-1',
        'selected_indices': [0]
    })
    assert missing_header_response.status_code == 400

    response = client.post(
        '/scrape/register-selected',
        json={
            'job_id': 'job-1',
            'selected_indices': [0]
        },
        headers={'X-CSRFToken': csrf_token},
    )

    assert response.status_code == 200
    assert response.json['ok'] is True


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


def test_scrape_jobs_api_returns_result_url_for_preview_jobs(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'jobs_preview_user')
    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'queued',
        'site': 'mercari',
        'result': None,
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': 1,
        'context': {
            'site_label': 'メルカリ',
            'detail_label': 'キーワード: preview',
            'limit': 10,
            'limit_label': '10件',
            'persist_to_db': False,
        },
        'created_at': 10.0,
        'finished_at': None,
        'user_id': user.id,
    }

    monkeypatch.setattr('routes.api.get_queue', lambda: fake_queue)

    response = client.get('/api/scrape/jobs')
    assert response.status_code == 200
    assert response.json['jobs'][0]['result_url'] == '/scrape?job_id=job-1'


def test_scrape_jobs_api_returns_result_url_for_persisted_jobs(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'jobs_persist_user')
    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'completed',
        'site': 'mercari',
        'result': {'items': [], 'persist_to_db': True},
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': None,
        'context': {
            'site_label': 'メルカリ',
            'detail_label': 'キーワード: persist',
            'limit': 10,
            'limit_label': '10件',
            'persist_to_db': True,
        },
        'created_at': 10.0,
        'finished_at': 12.0,
        'user_id': user.id,
    }

    monkeypatch.setattr('routes.api.get_queue', lambda: fake_queue)

    response = client.get('/api/scrape/jobs')
    assert response.status_code == 200
    assert response.json['jobs'][0]['result_url'] == '/scrape/result/job-1'


def test_scrape_jobs_api_hides_other_users_jobs(client, db_session, monkeypatch):
    login_user(client, db_session, 'jobs_owner_user')
    fake_queue = FakeQueue()
    fake_queue.jobs['job-1'] = {
        'job_id': 'job-1',
        'status': 'running',
        'site': 'mercari',
        'result': None,
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': None,
        'context': {'persist_to_db': False},
        'created_at': 10.0,
        'finished_at': None,
        'user_id': 9999,
    }

    monkeypatch.setattr('routes.api.get_queue', lambda: fake_queue)

    response = client.get('/api/scrape/jobs')
    assert response.status_code == 200
    assert response.json['jobs'] == []


def test_scrape_jobs_api_hides_dismissed_terminal_jobs(client, db_session, monkeypatch):
    user = login_user(client, db_session, 'jobs_dismissed_user')
    dismissed_at = utc_now()
    db_session.add(
        ScrapeJob(
            job_id='job-dismissed-1',
            logical_job_id='job-dismissed-1',
            status='completed',
            site='mercari',
            mode='persist',
            requested_by=user.id,
            result_summary='{"items_count": 1}',
            created_at=utc_now(),
            updated_at=utc_now(),
            finished_at=utc_now(),
            tracker_dismissed_at=dismissed_at,
        )
    )
    db_session.commit()

    fake_queue = FakeQueue()
    fake_queue.jobs['job-dismissed-1'] = {
        'job_id': 'job-dismissed-1',
        'status': 'completed',
        'site': 'mercari',
        'result': {'items': [], 'persist_to_db': True},
        'error': None,
        'elapsed_seconds': 0.1,
        'queue_position': None,
        'context': {'persist_to_db': True},
        'created_at': 10.0,
        'finished_at': 12.0,
        'user_id': user.id,
    }

    monkeypatch.setattr('routes.api.get_queue', lambda: fake_queue)

    response = client.get('/api/scrape/jobs')
    assert response.status_code == 200
    assert response.json['jobs'] == []


def test_scrape_job_dismiss_endpoint_marks_job_hidden(client, db_session):
    user = login_user(client, db_session, 'jobs_dismiss_endpoint_user')
    job = ScrapeJob(
        job_id='job-dismiss-api-1',
        logical_job_id='job-dismiss-api-1',
        status='completed',
        site='mercari',
        mode='persist',
        requested_by=user.id,
        result_summary='{"items_count": 1}',
        created_at=utc_now(),
        updated_at=utc_now(),
        finished_at=utc_now(),
    )
    db_session.add(job)
    db_session.commit()

    response = client.post('/api/scrape/jobs/job-dismiss-api-1/dismiss')
    assert response.status_code == 200
    assert response.json['ok'] is True

    db_session.expire_all()
    refreshed = db_session.query(ScrapeJob).filter_by(job_id='job-dismiss-api-1').one()
    assert refreshed.tracker_dismissed_at is not None


def test_scrape_jobs_dismiss_batch_endpoint_marks_multiple_jobs_hidden(client, db_session):
    user = login_user(client, db_session, 'jobs_dismiss_batch_user')
    finished_job = ScrapeJob(
        job_id='job-dismiss-batch-1',
        logical_job_id='job-dismiss-batch-1',
        status='completed',
        site='mercari',
        mode='persist',
        requested_by=user.id,
        result_summary='{"items_count": 1}',
        created_at=utc_now(),
        updated_at=utc_now(),
        finished_at=utc_now(),
    )
    failed_job = ScrapeJob(
        job_id='job-dismiss-batch-2',
        logical_job_id='job-dismiss-batch-2',
        status='failed',
        site='mercari',
        mode='persist',
        requested_by=user.id,
        error_message='failed',
        created_at=utc_now(),
        updated_at=utc_now(),
        finished_at=utc_now(),
    )
    running_job = ScrapeJob(
        job_id='job-dismiss-batch-3',
        logical_job_id='job-dismiss-batch-3',
        status='running',
        site='mercari',
        mode='persist',
        requested_by=user.id,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add_all([finished_job, failed_job, running_job])
    db_session.commit()

    response = client.post('/api/scrape/jobs/dismiss-batch', json={
        'job_ids': ['job-dismiss-batch-1', 'job-dismiss-batch-2', 'job-dismiss-batch-3']
    })
    assert response.status_code == 200
    assert response.json['ok'] is True
    assert response.json['dismissed_count'] == 2

    db_session.expire_all()
    refreshed_finished = db_session.query(ScrapeJob).filter_by(job_id='job-dismiss-batch-1').one()
    refreshed_failed = db_session.query(ScrapeJob).filter_by(job_id='job-dismiss-batch-2').one()
    refreshed_running = db_session.query(ScrapeJob).filter_by(job_id='job-dismiss-batch-3').one()
    assert refreshed_finished.tracker_dismissed_at is not None
    assert refreshed_failed.tracker_dismissed_at is not None
    assert refreshed_running.tracker_dismissed_at is None


def test_scrape_form_renders_tracker_config(client, db_session):
    login_user(client, db_session, 'scrape_form_render_user')

    response = client.get('/scrape')

    assert response.status_code == 200
    assert b'scrapePageConfig' in response.data
    assert b'globalScrapeTracker' in response.data
    assert b'globalScrapeTrackerPill' in response.data
    assert b'globalScrapeTrackerSheet' in response.data
    assert b'globalScrapeTrackerMobileList' in response.data
    assert b'globalScrapeTrackerDismissAll' in response.data
    assert b'globalScrapeTrackerSheetDismissAll' in response.data
    assert b'data-dismiss-batch-url=' in response.data
