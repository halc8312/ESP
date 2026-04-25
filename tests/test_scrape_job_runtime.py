from datetime import timedelta

import pytest

from database import SessionLocal
from models import ScrapeJob
from services.scrape_job_runtime import run_tracked_job
from services.scrape_job_store import create_job_record, get_job_record, mark_job_running, maybe_mark_job_stalled


def test_run_tracked_job_marks_job_completed(app):
    create_job_record(
        job_id="runtime-job-1",
        site="mercari",
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )

    result = run_tracked_job("runtime-job-1", lambda: {"items": [{"title": "ok"}], "persist_to_db": False})

    stored = get_job_record("runtime-job-1")
    assert result["items"][0]["title"] == "ok"
    assert stored["status"] == "completed"
    assert stored["result"]["items"][0]["title"] == "ok"


def test_run_tracked_job_marks_job_failed(app):
    create_job_record(
        job_id="runtime-job-2",
        site="mercari",
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )

    with pytest.raises(RuntimeError, match="boom"):
        run_tracked_job("runtime-job-2", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    stored = get_job_record("runtime-job-2")
    assert stored["status"] == "failed"
    assert stored["error"] == "boom"


def test_maybe_mark_job_stalled_converts_old_running_job(app):
    create_job_record(
        job_id="runtime-job-3",
        site="mercari",
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )
    mark_job_running("runtime-job-3")

    session = SessionLocal()
    try:
        record = session.query(ScrapeJob).filter_by(job_id="runtime-job-3").one()
        stale_time = record.updated_at - timedelta(seconds=1200)
        record.updated_at = stale_time
        record.started_at = stale_time
        session.commit()
    finally:
        session.close()

    assert maybe_mark_job_stalled("runtime-job-3", stall_timeout_seconds=60) is True

    stored = get_job_record("runtime-job-3")
    assert stored["status"] == "failed"
    assert stored["error_payload"]["kind"] == "job_stalled"
    assert "停止" in stored["error"]
