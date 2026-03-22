import pytest

from app import create_app
from services.queue_backend import get_queue_backend, serialize_scrape_job_for_api


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
