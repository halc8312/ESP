from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from database import create_isolated_session
from models import ScrapeHealthDelivery, ScrapeHealthObservation, ScrapeHealthState
from services import scrape_health as health


def record(outcome="failure", **kwargs):
    return health.record_scrape_observation(
        site="recordcity", route="search", outcome=outcome, **kwargs
    )


def row():
    return next(item for item in health.list_scrape_health() if item["site"] == "recordcity" and item["route"] == "search")


def dispatcher(monkeypatch, outcomes):
    calls = []

    def send(**payload):
        calls.append(payload)
        value = outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"status": value, "error_type": "TimeoutError" if value == "failed" else None}

    monkeypatch.setattr(health, "get_alert_dispatcher", lambda: SimpleNamespace(notify_scrape_issue_result=send))
    return calls


def test_all_sites_and_routes_unobserved_on_new_database(app):
    rows = health.list_scrape_health()
    assert len(rows) == 24
    assert len({(item["site"], item["route"]) for item in rows}) == 24
    assert all(item["status"] == "unobserved" for item in rows)
    assert all(item["last_success_at"] is None for item in rows)


def test_two_failed_jobs_open_persisted_incident(app):
    assert record(reason="captcha", error_count=50)
    assert row()["status"] == "degraded"
    assert row()["consecutive_failures"] == 1  # Not fifty products/jobs.
    assert record(reason="captcha", error_count=1)
    observed = row()
    assert observed["status"] == "failed"
    assert observed["incident_open"] is True
    assert observed["latest_delivery_status"] == "pending"
    with create_isolated_session() as session:
        assert session.query(ScrapeHealthObservation).count() == 2
        assert session.query(ScrapeHealthDelivery).count() == 1


def test_empty_observation_cannot_clear_failure_or_invent_success(app):
    record(reason="captcha")
    record(reason="captcha")
    failure_at = row()["last_failure_at"]
    record("no_observations", reason="empty_result")
    assert row()["status"] == "failed"
    assert row()["consecutive_failures"] == 2
    assert row()["reason"] == "captcha"
    assert row()["last_failure_at"] == failure_at
    assert row()["last_success_at"] is None
    record("success")  # A zero-item "success" is not extraction evidence.
    assert row()["status"] == "failed"


def test_mixed_batch_records_success_without_resolving_incident(app):
    record(reason="fetch_error", error_count=1)
    record(reason="fetch_error", success_count=49, error_count=1)
    assert row()["last_success_at"] is not None
    assert row()["status"] == "failed"
    assert row()["consecutive_failures"] == 2


def test_observed_success_closes_and_queues_recovery(app, monkeypatch):
    calls = dispatcher(monkeypatch, ["delivered", "delivered"])
    record(reason="fetch_error")
    record(reason="fetch_error")
    assert health.evaluate_scrape_health()["delivered"] == 1
    record("success", success_count=1)
    assert row()["status"] == "healthy"
    assert row()["consecutive_failures"] == 0
    assert row()["incident_open"] is False
    assert row()["last_failure_at"] is not None
    assert health.evaluate_scrape_health()["delivered"] == 1
    assert [call["event_type"] for call in calls] == ["scrape_health_incident", "scrape_health_recovery"]
    assert row()["latest_delivery_status"] == "delivered"


def test_new_success_supersedes_unsent_failure_notification(app, monkeypatch):
    calls = dispatcher(monkeypatch, ["delivered"])
    record(reason="fetch_error")
    record(reason="fetch_error")
    record("success", success_count=1)
    health.evaluate_scrape_health()
    assert len(calls) == 1
    assert calls[0]["event_type"] == "scrape_health_recovery"
    with create_isolated_session() as session:
        assert session.query(ScrapeHealthDelivery).order_by(ScrapeHealthDelivery.id).first().status == "superseded"


def test_delivery_failure_is_persisted_and_retried_only_when_due(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    calls = dispatcher(monkeypatch, ["failed", "delivered"])
    record(reason="timeout")
    record(reason="timeout")
    first = health.evaluate_scrape_health()
    assert first == {"status": "ok", "processed": 1, "delivered": 0}
    assert row()["latest_delivery_status"] == "failed"
    assert row()["latest_delivery_at"] is None
    assert health.evaluate_scrape_health()["processed"] == 0
    now[0] += timedelta(minutes=11)
    assert health.evaluate_scrape_health()["delivered"] == 1
    assert len(calls) == 2
    assert row()["latest_delivery_attempt_count"] == 2


def test_unconfigured_and_rate_limited_are_not_delivered(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    dispatcher(monkeypatch, ["unconfigured", "rate_limited", "delivered"])
    record()
    record()
    assert health.evaluate_scrape_health()["delivered"] == 0
    assert row()["latest_delivery_status"] == "unconfigured"
    now[0] += timedelta(minutes=61)
    health.evaluate_scrape_health()
    assert row()["latest_delivery_status"] == "rate_limited"
    now[0] += timedelta(minutes=6)
    assert health.evaluate_scrape_health()["delivered"] == 1


def test_claim_prevents_concurrent_dispatch_and_expired_lease_can_retry(app):
    record()
    record()
    with create_isolated_session() as session:
        delivery_id = session.query(ScrapeHealthDelivery.id).scalar()
    now = health.utc_now()
    claim = health._claim_delivery(delivery_id, now)
    assert claim is not None
    assert health._claim_delivery(delivery_id, now) is None
    next_claim = health._claim_delivery(delivery_id, now + timedelta(minutes=3))
    assert next_claim is not None
    assert next_claim["token"] != claim["token"]


def test_stale_means_evidence_old_not_automatically_new_failure(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    record("success", success_count=1)
    now[0] += timedelta(hours=25)
    record("no_observations", reason="empty_result")
    assert row()["status"] == "stale"
    assert row()["consecutive_failures"] == 0
    assert row()["latest_delivery_status"] is None


def test_history_is_bounded_and_reason_text_redacted(app, monkeypatch):
    monkeypatch.setattr(health, "HISTORY_PER_ROUTE", 3)
    secret = "https://secret-webhook.example/token?user=private"
    for _ in range(5):
        assert record(reason=secret)
    with create_isolated_session() as session:
        observations = session.query(ScrapeHealthObservation).all()
        assert len(observations) == 3
        assert {item.reason for item in observations} == {"unknown"}
        assert secret not in str(row())
        for model in (ScrapeHealthState, ScrapeHealthObservation, ScrapeHealthDelivery):
            columns = {column.name for column in inspect(model).columns}
            assert not columns.intersection({"source_url", "user_id", "keyword", "title", "cookie", "webhook_url"})
    assert health.record_scrape_observation(site=secret, route="search", outcome="failure") is False


def test_database_failure_does_not_escape_or_log_raw_error(app, monkeypatch, caplog):
    secret = "postgres://name:secret@host/database"

    def broken():
        raise RuntimeError(secret)

    monkeypatch.setattr(health, "create_isolated_session", broken)
    assert record() is False
    assert all(item["status"] == "monitoring_unavailable" for item in health.list_scrape_health())
    assert health.evaluate_scrape_health()["status"] == "error"
    assert secret not in caplog.text


def test_retry_is_bounded_and_expiration_is_visible(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    monkeypatch.setattr(health, "MAX_FAILURE_ATTEMPTS", 2)
    dispatcher(monkeypatch, ["failed", "failed"])
    record()
    record()
    health.evaluate_scrape_health()
    now[0] += timedelta(hours=1)
    health.evaluate_scrape_health()
    assert row()["latest_delivery_status"] == "exhausted"
    now[0] += timedelta(hours=1)
    assert health.evaluate_scrape_health()["processed"] == 0
    record("success", success_count=1)
    now[0] += timedelta(days=8)
    health.evaluate_scrape_health()
    assert row()["latest_delivery_status"] == "expired"


def test_migration_create_all_compatibility_and_idempotence(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20260905_0021_add_scrape_health.py"
    spec = importlib.util.spec_from_file_location("scrape_health_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            # Bootstrap may have created one of the tables already.
            ScrapeHealthState.__table__.create(connection)
            migration.upgrade()
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) == {
                "scrape_health_states", "scrape_health_observations", "scrape_health_deliveries"
            }
            for model in (ScrapeHealthState, ScrapeHealthObservation, ScrapeHealthDelivery):
                migrated = {column["name"] for column in inspect(connection).get_columns(model.__tablename__)}
                assert migrated == {column.name for column in inspect(model).columns}
            migration.downgrade()
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    finally:
        engine.dispose()


def test_evaluator_cannot_duplicate_an_active_concurrent_claim(app, monkeypatch):
    calls = []
    record()
    record()

    def send(**payload):
        calls.append(payload)
        # Simulate a competing evaluator while the first has released its
        # database transaction and is waiting on the external HTTP response.
        assert health.evaluate_scrape_health()["processed"] == 0
        return {"status": "delivered", "error_type": None}

    monkeypatch.setattr(health, "get_alert_dispatcher", lambda: SimpleNamespace(notify_scrape_issue_result=send))
    assert health.evaluate_scrape_health()["delivered"] == 1
    assert len(calls) == 1


def test_inconclusive_batch_keeps_actual_success_evidence_without_recovery(app):
    record(reason="fetch_error")
    record(reason="fetch_error")
    assert record("no_observations", reason="inconclusive", success_count=3)
    assert row()["last_success_at"] is not None
    assert row()["incident_open"] is True
    assert row()["consecutive_failures"] == 2
    with create_isolated_session() as session:
        latest = session.query(ScrapeHealthObservation).order_by(ScrapeHealthObservation.id.desc()).first()
        assert latest.success_count == 3
        assert latest.outcome == "no_observations"


def test_failed_inflight_incident_cannot_retry_after_recovery(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    calls = []

    def send(**payload):
        calls.append(payload["event_type"])
        if payload["event_type"] == "scrape_health_incident":
            assert record("success", success_count=1)
            return {"status": "failed", "error_type": "TimeoutError"}
        return {"status": "delivered", "error_type": None}

    monkeypatch.setattr(health, "get_alert_dispatcher", lambda: SimpleNamespace(notify_scrape_issue_result=send))
    record()
    record()
    health.evaluate_scrape_health()
    assert row()["status"] == "healthy"
    assert health.evaluate_scrape_health()["delivered"] == 1
    now[0] += timedelta(minutes=11)
    assert health.evaluate_scrape_health()["processed"] == 0
    assert calls == ["scrape_health_incident", "scrape_health_recovery"]
    with create_isolated_session() as session:
        assert session.query(ScrapeHealthDelivery).order_by(ScrapeHealthDelivery.id).first().status == "superseded"


def test_failed_inflight_recovery_cannot_retry_after_new_incident(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    calls = []

    def send(**payload):
        calls.append((payload["event_type"], payload["details"]["incident_number"]))
        if payload["event_type"] == "scrape_health_recovery":
            assert record(reason="timeout")
            assert record(reason="timeout")
            return {"status": "failed", "error_type": "TimeoutError"}
        return {"status": "delivered", "error_type": None}

    monkeypatch.setattr(health, "get_alert_dispatcher", lambda: SimpleNamespace(notify_scrape_issue_result=send))
    record()
    record()
    health.evaluate_scrape_health()
    record("success", success_count=1)
    health.evaluate_scrape_health()
    assert row()["incident_open"] is True
    health.evaluate_scrape_health()
    now[0] += timedelta(minutes=11)
    assert health.evaluate_scrape_health()["processed"] == 0
    assert calls == [("scrape_health_incident", 1), ("scrape_health_recovery", 1), ("scrape_health_incident", 2)]
    with create_isolated_session() as session:
        recovery = session.query(ScrapeHealthDelivery).filter_by(event_type="recovery").one()
        assert recovery.status == "superseded"


def test_recovered_claim_is_rechecked_before_http(app, monkeypatch):
    calls = dispatcher(monkeypatch, [])
    record()
    record()
    with create_isolated_session() as session:
        delivery_id = session.query(ScrapeHealthDelivery.id).scalar()
    claim = health._claim_delivery(delivery_id, health.utc_now())
    record("success", success_count=1)
    assert health._deliver(claim) == "superseded"
    assert calls == []


def test_expired_obsolete_claim_is_not_reclaimed_after_worker_crash(app):
    record()
    record()
    with create_isolated_session() as session:
        delivery_id = session.query(ScrapeHealthDelivery.id).scalar()
    now = health.utc_now()
    assert health._claim_delivery(delivery_id, now) is not None
    record("success", success_count=1)
    assert health._claim_delivery(delivery_id, now + timedelta(minutes=3)) is None
    with create_isolated_session() as session:
        assert session.get(ScrapeHealthDelivery, delivery_id).status == "superseded"


def test_single_failure_defers_recovery_until_next_actual_success(app, monkeypatch):
    calls = dispatcher(monkeypatch, ["delivered", "delivered"])
    record()
    record()
    assert health.evaluate_scrape_health()["delivered"] == 1
    record("success", success_count=1)
    record(reason="timeout")
    assert row()["status"] == "degraded"
    assert health.evaluate_scrape_health()["processed"] == 0
    assert row()["latest_delivery_status"] == "deferred"
    assert len(calls) == 1
    record("success", success_count=1)
    assert row()["latest_delivery_status"] == "pending"
    assert health.evaluate_scrape_health()["delivered"] == 1
    assert [item["event_type"] for item in calls] == ["scrape_health_incident", "scrape_health_recovery"]
    # Later success is additional evidence, never another recovery delivery.
    record("success", success_count=1)
    assert health.evaluate_scrape_health()["processed"] == 0
    assert len(calls) == 2


def test_recovery_stays_deferred_without_success_then_new_incident_supersedes(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    calls = dispatcher(monkeypatch, ["delivered", "delivered"])
    record()
    record()
    health.evaluate_scrape_health()
    record("success", success_count=1)
    record(reason="timeout")
    health.evaluate_scrape_health()
    assert row()["latest_delivery_status"] == "deferred"
    now[0] += timedelta(minutes=6)
    record("no_observations", reason="empty_result")
    assert health.evaluate_scrape_health()["processed"] == 0
    assert row()["latest_delivery_status"] == "deferred"
    assert len(calls) == 1
    record(reason="timeout")  # Second failing observation opens incident2.
    assert health.evaluate_scrape_health()["delivered"] == 1
    assert [item["details"]["incident_number"] for item in calls] == [1, 2]
    with create_isolated_session() as session:
        recovery = session.query(ScrapeHealthDelivery).filter_by(event_type="recovery").one()
        assert recovery.status == "superseded"


def test_single_failure_before_recovery_http_defers_without_sending(app, monkeypatch):
    calls = dispatcher(monkeypatch, ["delivered"])
    record()
    record()
    health.evaluate_scrape_health()
    record("success", success_count=1)
    with create_isolated_session() as session:
        delivery_id = session.query(ScrapeHealthDelivery.id).filter_by(event_type="recovery").scalar()
    claim = health._claim_delivery(delivery_id, health.utc_now())
    record(reason="timeout")
    assert health._deliver(claim) == "deferred"
    assert len(calls) == 1
    assert row()["latest_delivery_status"] == "deferred"


def test_failed_recovery_http_during_single_failure_defers_then_resumes(app, monkeypatch):
    calls = []

    def send(**payload):
        calls.append(payload["event_type"])
        if len(calls) == 2:
            assert record(reason="timeout")
            return {"status": "failed", "error_type": "TimeoutError"}
        return {"status": "delivered", "error_type": None}

    monkeypatch.setattr(health, "get_alert_dispatcher", lambda: SimpleNamespace(notify_scrape_issue_result=send))
    record()
    record()
    health.evaluate_scrape_health()
    record("success", success_count=1)
    assert health.evaluate_scrape_health()["delivered"] == 0
    assert row()["latest_delivery_status"] == "deferred"
    record("success", success_count=1)
    assert health.evaluate_scrape_health()["delivered"] == 1
    assert calls == ["scrape_health_incident", "scrape_health_recovery", "scrape_health_recovery"]


def test_deferred_recovery_still_expires_after_seven_days(app, monkeypatch):
    now = [datetime(2026, 9, 5)]
    monkeypatch.setattr(health, "utc_now", lambda: now[0])
    calls = dispatcher(monkeypatch, ["delivered"])
    record()
    record()
    health.evaluate_scrape_health()
    record("success", success_count=1)
    record(reason="timeout")
    health.evaluate_scrape_health()
    assert row()["latest_delivery_status"] == "deferred"
    now[0] += timedelta(days=8)
    health.evaluate_scrape_health()
    assert row()["latest_delivery_status"] == "expired"
    record("success", success_count=1)
    assert health.evaluate_scrape_health()["processed"] == 0
    assert len(calls) == 1
