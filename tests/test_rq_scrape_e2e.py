import uuid

import pytest
from redis import Redis

from app import create_worker_app
from models import User
from services.worker_runtime import run_worker


def _login_user(client, db_session, username="rq_e2e_user"):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post(
        "/login",
        data={
            "username": username,
            "password": "testpassword",
        },
    )
    return user


def _redis_available(redis_url: str) -> bool:
    try:
        Redis.from_url(redis_url).ping()
        return True
    except Exception:
        return False


def test_preview_job_round_trips_from_route_through_rq_worker_and_status_api(client, db_session, monkeypatch):
    redis_url = "redis://localhost:6379/0"
    if not _redis_available(redis_url):
        pytest.skip("Local Redis is not available for RQ end-to-end smoke")

    queue_name = f"scrape-e2e-{uuid.uuid4().hex}"
    username = f"rq_e2e_{uuid.uuid4().hex[:8]}"
    user = _login_user(client, db_session, username)

    app = client.application
    app.config.update(
        {
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": redis_url,
            "SCRAPE_QUEUE_NAME": queue_name,
        }
    )

    fake_items = [
        {
            "url": "https://jp.mercari.com/item/m-rq-e2e-1",
            "title": "RQ Preview Item",
            "price": 1980,
            "status": "on_sale",
            "description": "preview item from rq e2e",
            "image_urls": ["https://img.example.com/rq-e2e-1.jpg"],
            "variants": [],
        }
    ]

    monkeypatch.setattr("jobs.scrape_tasks.scrape_search_result", lambda *args, **kwargs: list(fake_items))
    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr(
        "jobs.scrape_tasks.filter_items_by_price",
        lambda items, price_min, price_max: (items, 0),
    )
    monkeypatch.setattr(
        "jobs.scrape_tasks.save_scraped_items_to_db",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preview mode should not persist")),
    )

    response = client.post(
        "/scrape/run",
        data={
            "site": "mercari",
            "keyword": "rq smoke",
            "response_mode": "preview",
            "limit": "10",
        },
    )

    assert response.status_code == 202
    job_id = response.json["job_id"]
    assert response.json["status"] == "queued"
    assert response.json["context"]["persist_to_db"] is False

    worker_app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "REDIS_URL": redis_url,
            "SCRAPE_QUEUE_NAME": queue_name,
            "RQ_BURST": True,
            "WARM_BROWSER_POOL": False,
        }
    )

    assert run_worker(worker_app) == 0

    status_response = client.get(f"/api/scrape/status/{job_id}")
    assert status_response.status_code == 200
    assert status_response.json["status"] == "completed"
    assert status_response.json["result"]["items"][0]["title"] == "RQ Preview Item"
    assert status_response.json["result"]["persist_to_db"] is False
    assert status_response.json["result_summary"]["items_count"] == 1

    jobs_response = client.get("/api/scrape/jobs")
    assert jobs_response.status_code == 200
    assert jobs_response.json["jobs"][0]["job_id"] == job_id
    assert jobs_response.json["jobs"][0]["status"] == "completed"
    assert jobs_response.json["jobs"][0]["context"]["persist_to_db"] is False
    assert jobs_response.json["jobs"][0]["site"] == "mercari"

    assert db_session.query(User).filter_by(id=user.id).one().username == username
