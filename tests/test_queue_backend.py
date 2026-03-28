import pytest
import sys
from datetime import datetime

from app import create_app
from models import User
from services.scrape_job_store import create_job_record, mark_job_running
from services.queue_backend import _job_sort_key, get_queue_backend, serialize_scrape_job_for_api


def _create_user(session, user_id: int = 1, username: str = "queue-backend-user") -> User:
    user = User(id=user_id, username=username)
    user.set_password("password123")
    session.add(user)
    session.commit()
    return user


def test_serialize_scrape_job_for_api_keeps_preview_route_shape():
    app = create_app(runtime_role="test", config_overrides={"TESTING": True})
    with app.test_request_context():
        payload = {
            "job_id": "job-1",
            "site": "mercari",
            "status": "queued",
            "result": None,
            "error": None,
            "elapsed_seconds": 0.1,
            "queue_position": 1,
            "context": {
                "persist_to_db": False,
            },
            "created_at": 10.0,
            "finished_at": None,
        }

        serialized = serialize_scrape_job_for_api(payload)

    assert serialized == {
        "job_id": "job-1",
        "site": "mercari",
        "status": "queued",
        "result": None,
        "error": None,
        "elapsed_seconds": 0.1,
        "queue_position": 1,
        "context": {"persist_to_db": False},
        "created_at": 10.0,
        "finished_at": None,
        "result_url": "/scrape?job_id=job-1",
        "result_summary": None,
    }


def test_get_queue_backend_defaults_to_inmemory(app):
    backend = get_queue_backend()
    assert hasattr(backend, "enqueue")
    assert hasattr(backend, "get_status")
    assert hasattr(backend, "get_jobs_for_user")


def test_get_queue_backend_rejects_unknown_backend():
    app = create_app(
        runtime_role="test",
        config_overrides={
            "TESTING": True,
            "SCRAPE_QUEUE_BACKEND": "unsupported",
        },
    )

    with app.app_context():
        with pytest.raises(RuntimeError, match="Unsupported SCRAPE_QUEUE_BACKEND"):
            get_queue_backend()


def test_get_queue_backend_requires_rq_dependencies(app, monkeypatch):
    with app.app_context():
        from database import SessionLocal

        session = SessionLocal()
        try:
            _create_user(session, username="queue-rq-deps")
        finally:
            session.close()

        app.config.update(
            {
                "SCRAPE_QUEUE_BACKEND": "rq",
                "REDIS_URL": "redis://localhost:6379/0",
            }
        )
        backend = get_queue_backend()
        monkeypatch.setitem(sys.modules, "rq", None)
        monkeypatch.setitem(sys.modules, "redis", None)
        with pytest.raises(RuntimeError, match="requires `rq` and `redis` packages"):
            backend.enqueue(
                site="mercari",
                task_fn=lambda: {},
                user_id=1,
                context={"persist_to_db": False},
                request_payload={"site": "mercari", "persist_to_db": False},
                mode="preview",
            )


def test_get_queue_backend_maps_stalled_running_job_to_failed(app):
    with app.app_context():
        from database import SessionLocal
        from datetime import timedelta
        from models import ScrapeJob

        session = SessionLocal()
        try:
            _create_user(session, username="queue-stalled")
        finally:
            session.close()

        app.config.update(
            {
                "SCRAPE_QUEUE_BACKEND": "rq",
                "SCRAPE_JOB_STALL_TIMEOUT_SECONDS": 60,
            }
        )
        create_job_record(
            job_id="stalled-job-1",
            site="mercari",
            user_id=1,
            context={"persist_to_db": False},
            request_payload={"site": "mercari", "persist_to_db": False},
            mode="preview",
        )
        mark_job_running("stalled-job-1")

        session = SessionLocal()
        try:
            record = session.query(ScrapeJob).filter_by(job_id="stalled-job-1").one()
            stale_time = record.updated_at - timedelta(seconds=3600)
            record.updated_at = stale_time
            record.started_at = stale_time
            session.commit()
        finally:
            session.close()

        backend = get_queue_backend()
        status = backend.get_status("stalled-job-1", user_id=1)

    assert status is not None
    assert status["status"] == "failed"
    assert "停止" in status["error"]


def test_get_queue_backend_maps_missing_rq_job_to_failed(app, monkeypatch):
    with app.app_context():
        from database import SessionLocal
        from datetime import timedelta
        from models import ScrapeJob

        session = SessionLocal()
        try:
            _create_user(session, username="queue-orphan-old")
        finally:
            session.close()

        app.config.update(
            {
                "SCRAPE_QUEUE_BACKEND": "rq",
                "SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS": 60,
            }
        )
        create_job_record(
            job_id="orphan-job-1",
            site="mercari",
            user_id=1,
            context={"persist_to_db": False},
            request_payload={"site": "mercari", "persist_to_db": False},
            mode="preview",
        )

        session = SessionLocal()
        try:
            record = session.query(ScrapeJob).filter_by(job_id="orphan-job-1").one()
            stale_time = record.updated_at - timedelta(seconds=3600)
            record.updated_at = stale_time
            record.created_at = stale_time
            session.commit()
        finally:
            session.close()

        backend = get_queue_backend()
        monkeypatch.setattr(backend, "_rq_job_exists", lambda job_id: False)
        status = backend.get_status("orphan-job-1", user_id=1)

    assert status is not None
    assert status["status"] == "failed"
    assert "見つかりません" in status["error"]


def test_get_queue_backend_keeps_fresh_missing_rq_job_non_terminal(app, monkeypatch):
    with app.app_context():
        from database import SessionLocal

        session = SessionLocal()
        try:
            _create_user(session, username="queue-orphan-fresh")
        finally:
            session.close()

        app.config.update(
            {
                "SCRAPE_QUEUE_BACKEND": "rq",
                "SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS": 3600,
            }
        )
        create_job_record(
            job_id="orphan-job-2",
            site="mercari",
            user_id=1,
            context={"persist_to_db": False},
            request_payload={"site": "mercari", "persist_to_db": False},
            mode="preview",
        )

        backend = get_queue_backend()
        monkeypatch.setattr(backend, "_rq_job_exists", lambda job_id: False)
        status = backend.get_status("orphan-job-2", user_id=1)

    assert status is not None
    assert status["status"] == "queued"


def test_job_sort_key_handles_datetime_values():
    value = datetime(2026, 3, 24, 12, 0, 0)
    assert _job_sort_key(value) == value.timestamp()
