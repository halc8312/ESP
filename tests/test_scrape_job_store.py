from models import User
from services.scrape_job_store import (
    create_job_record,
    get_job_backlog_snapshot,
    get_job_record,
    list_job_records_for_user,
    mark_job_completed,
    maybe_mark_job_orphaned,
    mark_job_running,
    reconcile_stalled_jobs,
)


def test_scrape_job_store_round_trip(app, db_session):
    user = User(username="job_store_user")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()

    create_job_record(
        job_id="job-store-1",
        site="mercari",
        user_id=user.id,
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False, "keyword": "preview"},
        mode="preview",
    )

    queued = get_job_record("job-store-1", user_id=user.id)
    assert queued is not None
    assert queued["status"] == "queued"
    assert queued["context"]["persist_to_db"] is False

    mark_job_running("job-store-1")
    mark_job_completed(
        "job-store-1",
        {
            "items": [{"title": "Preview Item"}],
            "new_count": 0,
            "updated_count": 0,
            "excluded_count": 0,
            "persist_to_db": False,
        },
    )

    completed = get_job_record("job-store-1", user_id=user.id)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["items"][0]["title"] == "Preview Item"
    assert completed["result_summary"] == {
        "items_count": 1,
        "excluded_count": 0,
        "new_count": 0,
        "updated_count": 0,
    }

    jobs = list_job_records_for_user(user.id, limit=5)
    assert [job["job_id"] for job in jobs] == ["job-store-1"]


def test_reconcile_stalled_jobs_marks_only_expired_running_jobs(app, db_session):
    user = User(username="job_store_reconcile_user")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()

    create_job_record(
        job_id="job-stale-1",
        site="mercari",
        user_id=user.id,
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )
    create_job_record(
        job_id="job-fresh-1",
        site="mercari",
        user_id=user.id,
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )
    mark_job_running("job-stale-1")
    mark_job_running("job-fresh-1")

    from datetime import timedelta

    from database import SessionLocal
    from models import ScrapeJob

    session = SessionLocal()
    try:
        stale_record = session.query(ScrapeJob).filter_by(job_id="job-stale-1").one()
        fresh_record = session.query(ScrapeJob).filter_by(job_id="job-fresh-1").one()
        stale_record.updated_at = stale_record.updated_at - timedelta(seconds=3600)
        stale_record.started_at = stale_record.started_at - timedelta(seconds=3600)
        session.commit()
        fresh_updated_at = fresh_record.updated_at
    finally:
        session.close()

    reconciled = reconcile_stalled_jobs(stall_timeout_seconds=60)

    assert reconciled == ["job-stale-1"]

    stale_job = get_job_record("job-stale-1", user_id=user.id)
    fresh_job = get_job_record("job-fresh-1", user_id=user.id)

    assert stale_job["status"] == "failed"
    assert "停止" in stale_job["error"]
    assert fresh_job["status"] == "running"
    assert fresh_job["updated_at"] == fresh_updated_at


def test_maybe_mark_job_orphaned_marks_old_non_terminal_job(app, db_session):
    user = User(username="job_store_orphan_user")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()

    create_job_record(
        job_id="job-orphan-1",
        site="mercari",
        user_id=user.id,
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )

    from datetime import timedelta

    from database import SessionLocal
    from models import ScrapeJob

    session = SessionLocal()
    try:
        record = session.query(ScrapeJob).filter_by(job_id="job-orphan-1").one()
        stale_time = record.updated_at - timedelta(seconds=3600)
        record.updated_at = stale_time
        record.created_at = stale_time
        session.commit()
    finally:
        session.close()

    assert maybe_mark_job_orphaned("job-orphan-1", orphan_timeout_seconds=60) is True

    job = get_job_record("job-orphan-1", user_id=user.id)
    assert job["status"] == "failed"
    assert job["error_payload"]["kind"] == "job_orphaned"


def test_get_job_backlog_snapshot_reports_oldest_queued_and_running(app, db_session):
    user = User(username="job_store_backlog_user")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()

    create_job_record(
        job_id="job-backlog-queued",
        site="mercari",
        user_id=user.id,
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )
    create_job_record(
        job_id="job-backlog-running",
        site="mercari",
        user_id=user.id,
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )
    mark_job_running("job-backlog-running")

    from datetime import timedelta

    from database import SessionLocal
    from models import ScrapeJob

    session = SessionLocal()
    try:
        queued_record = session.query(ScrapeJob).filter_by(job_id="job-backlog-queued").one()
        running_record = session.query(ScrapeJob).filter_by(job_id="job-backlog-running").one()
        queued_record.created_at = queued_record.created_at - timedelta(seconds=120)
        running_record.updated_at = running_record.updated_at - timedelta(seconds=240)
        running_record.started_at = running_record.started_at - timedelta(seconds=240)
        session.commit()
    finally:
        session.close()

    snapshot = get_job_backlog_snapshot()

    assert snapshot["queued_count"] >= 1
    assert snapshot["running_count"] >= 1
    assert snapshot["oldest_queued_job_id"] == "job-backlog-queued"
    assert snapshot["oldest_running_job_id"] == "job-backlog-running"
    assert snapshot["oldest_queued_age_seconds"] >= 120
    assert snapshot["oldest_running_age_seconds"] >= 240
