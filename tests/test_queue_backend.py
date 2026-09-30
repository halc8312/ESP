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


def test_get_queue_backend_selects_rq_backend_from_app_config(app):
    with app.app_context():
        app.config.update(
            {
                "SCRAPE_QUEUE_BACKEND": "rq",
                "REDIS_URL": "redis://example.test:6379/9",
                "SCRAPE_QUEUE_NAME": "existing-web-cutover",
            }
        )

        backend = get_queue_backend()

    assert backend.__class__.__name__ == "RQQueueBackend"
    assert backend._redis_url == "redis://example.test:6379/9"
    assert backend._queue_name == "existing-web-cutover"


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


@pytest.mark.parametrize("queue_status", ["started", "unavailable"])
def test_get_queue_backend_maps_stalled_running_job_to_failed(app, monkeypatch, queue_status):
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
        # This job was never enqueued in Redis. An unrelated localhost Redis
        # (including CI's governor-test service) would correctly report it as
        # missing, which tests orphan handling rather than heartbeat expiry.
        def observed_queue_status(job_id):
            if queue_status == "unavailable":
                raise ConnectionError("test Redis unavailable")
            return queue_status
        monkeypatch.setattr(backend, "_rq_job_status", observed_queue_status)
        status = backend.get_status("stalled-job-1", user_id=1)

    assert status is not None
    assert status["status"] == "failed"
    assert status["error_payload"]["kind"] == "job_stalled"
    assert "停止" in status["error"]


@pytest.mark.parametrize("durable_status", ["queued", "running"])
def test_get_queue_backend_maps_missing_rq_job_to_failed(app, monkeypatch, durable_status):
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
        if durable_status == "running":
            mark_job_running("orphan-job-1")

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
        monkeypatch.setattr(backend, "_rq_job_status", lambda job_id: "missing")
        status = backend.get_status("orphan-job-1", user_id=1)

    assert status is not None
    assert status["status"] == "failed"
    assert status["error_payload"]["kind"] == "job_orphaned"
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
        monkeypatch.setattr(backend, "_rq_job_status", lambda job_id: "missing")
        status = backend.get_status("orphan-job-2", user_id=1)

    assert status is not None
    assert status["status"] == "queued"


def test_job_sort_key_handles_datetime_values():
    value = datetime(2026, 3, 24, 12, 0, 0)
    assert _job_sort_key(value) == value.timestamp()


@pytest.mark.parametrize("queue_status", ["failed", "stopped", "canceled"])
def test_rq_terminal_state_reconciles_immediately_and_preserves_partial(app, db_session, monkeypatch, queue_status):
    from services.queue_backend import RQQueueBackend
    from services.scrape_job_store import get_job_record, mark_job_progress
    _create_user(db_session)
    create_job_record("rq-terminal", "recordcity", user_id=1, context={"persist_to_db": False})
    mark_job_running("rq-terminal")
    mark_job_progress("rq-terminal", {"items": [{"title": "retained"}]}, {"items_count": 1})
    backend = RQQueueBackend("redis://unused", "scrape")
    monkeypatch.setattr(backend, "_rq_job_status", lambda job_id: queue_status)
    observations = []
    monkeypatch.setattr("services.scrape_observation.record_observation_safely", lambda **kw: observations.append(kw))
    status = backend.get_status("rq-terminal", user_id=1)
    assert status["status"] == "failed"
    assert status["error_payload"]["kind"] == "worker_" + queue_status
    assert status["result"]["items"][0]["title"] == "retained"
    backend.get_status("rq-terminal", user_id=1)
    assert len(observations) == 1
    assert get_job_record("rq-terminal")["status"] == "failed"


def test_rq_redis_unavailable_does_not_invent_terminal_failure(app, monkeypatch):
    from services.queue_backend import RQQueueBackend
    create_job_record("rq-unknown", "recordcity")
    mark_job_running("rq-unknown")
    backend = RQQueueBackend("redis://unused", "scrape")
    def unavailable(job_id):
        raise ConnectionError("unavailable")
    monkeypatch.setattr(backend, "_rq_job_status", unavailable)
    assert backend.get_status("rq-unknown")["status"] == "running"


def test_rq_status_of_another_user_is_not_read_or_reconciled(app, db_session, monkeypatch):
    from services.queue_backend import RQQueueBackend
    _create_user(db_session, user_id=2)
    create_job_record("rq-other-user", "recordcity", user_id=2)
    backend = RQQueueBackend("redis://unused", "scrape")
    monkeypatch.setattr(backend, "_rq_job_status", lambda job_id: pytest.fail("cross-tenant queue lookup"))
    assert backend.get_status("rq-other-user", user_id=1) is None


def test_terminal_durable_failure_wins_over_late_inmemory_completion():
    from services.queue_backend import _merge_job_payload
    merged = _merge_job_payload(
        {"job_id": "late", "status": "failed", "error": "watchdog", "result": {"items": [1], "partial": True}},
        {"job_id": "late", "status": "completed", "result": {"items": [1, 2]}},
    )
    assert merged["status"] == "failed"
    assert merged["error"] == "watchdog"
    assert merged["result"]["items"] == [1]
