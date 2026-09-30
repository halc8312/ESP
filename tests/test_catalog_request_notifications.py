"""All notification transport is mocked; no test sends real email."""
from datetime import timedelta
import json
from types import SimpleNamespace
import uuid

import pytest

from models import CatalogRequest, PriceList, PriceListItem, Product, User, Variant
from models_mail import CatalogRequestNotification as Notification
from services import catalog_request_notifications as notes
from services.mail_service import MailResult, ResendMailer
from time_utils import utc_now

QUEUE_PRESENCE_IMPLEMENTATION = notes._queue_presence


@pytest.fixture(autouse=True)
def disabled_and_offline(monkeypatch):
    for key in ("CATALOG_REQUEST_NOTIFICATIONS_ENABLED", "MAIL_ENABLED", "MAIL_PROVIDER", "MAIL_FROM", "RESEND_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ResendMailer, "send", lambda *args, **kwargs: pytest.fail("real email forbidden"))
    monkeypatch.setattr(notes, "_queue_presence", lambda *args: "gone")


def enable(monkeypatch):
    monkeypatch.setenv("CATALOG_REQUEST_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("MAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_mock_secret")


@pytest.fixture
def catalog(db_session, monkeypatch):
    owner = User(username="note_owner", email="owner@EXAMPLE.test", password_hash="test")
    other = User(username="note_other", email="other@example.test", password_hash="test")
    db_session.add_all([owner, other])
    db_session.flush()
    product = Product(user_id=owner.id, site="mercari", source_url="https://jp.mercari.com/item/m12345678901",
                      last_title="Public item", last_price=777, selling_price=1200, last_status="on_sale",
                      variants=[Variant(price=777, inventory_qty=5)])
    price_list = PriceList(user_id=owner.id, name="Public list", token=uuid.uuid4().hex)
    db_session.add_all([product, price_list])
    db_session.flush()
    db_session.add(PriceListItem(price_list_id=price_list.id, product_id=product.id))
    db_session.commit()
    jobs = []
    monkeypatch.setattr(notes, "_dispatch", lambda *args: jobs.append(args))
    return SimpleNamespace(owner_id=owner.id, other_id=other.id, product_id=product.id, token=price_list.token, jobs=jobs)


def submit(client, catalog, **overrides):
    payload = {"submission_key": uuid.uuid4().hex, "buyer_instagram": "buyer",
               "buyer_name": "Private buyer name", "message": "Private buyer message",
               "items": [{"product_id": catalog.product_id, "quantity": 1, "expected_price_jpy": 1200}], **overrides}
    return client.post(f"/catalog/{catalog.token}/requests", json=payload), payload


def row(db_session):
    db_session.expire_all()
    return db_session.query(Notification).one()


def fake_sender(monkeypatch, result=None):
    sent = []
    def send(self, message, *, idempotency_key):
        sent.append((self._settings.sender, message, idempotency_key))
        return result or MailResult("accepted", "api_accepted", 200, "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(ResendMailer, "send", send)
    return sent


def test_disabled_request_is_saved_once_without_historical_replay(client, db_session, catalog, monkeypatch):
    response, payload = submit(client, catalog)
    assert response.status_code == 201
    assert row(db_session).status == "disabled"
    retry = client.post(f"/catalog/{catalog.token}/requests", json=payload)
    assert retry.status_code == 200 and db_session.query(Notification).count() == 1
    enable(monkeypatch)
    assert notes.recover_request_notifications()["enqueued"] == 0
    assert catalog.jobs == []
    assert row(db_session).status == "disabled"


@pytest.mark.parametrize("missing", ["CATALOG_REQUEST_NOTIFICATIONS_ENABLED", "MAIL_ENABLED", "MAIL_PROVIDER"])
def test_both_opt_in_flags_and_api_provider_are_required(client, db_session, catalog, monkeypatch, missing):
    enable(monkeypatch)
    monkeypatch.delenv(missing)
    assert submit(client, catalog)[0].status_code == 201
    assert row(db_session).status in {"disabled", "unconfigured"}
    assert not catalog.jobs


def test_one_frozen_minimal_email_per_request_not_per_item(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    response, payload = submit(client, catalog)
    assert response.status_code == 201 and len(catalog.jobs) == 1
    note = row(db_session)
    assert note.recipient == "owner@example.test"
    for private in ("mercari", "recordcity", "https://", "777", "buyer", "Private buyer", "source_url", "last_price"):
        assert private not in note.body
    sent = fake_sender(monkeypatch)
    assert notes.run_notification_job(*catalog.jobs[0]) == {"status": "accepted", "receipt_verified": False}
    assert len(sent) == 1
    assert notes.run_notification_job(*catalog.jobs[0])["status"] == "stale"
    assert row(db_session).attempt_count == 1
    assert row(db_session).status == "accepted"
    assert client.post(f"/catalog/{catalog.token}/requests", json=payload).status_code == 200
    assert len(sent) == 1 and db_session.query(Notification).count() == 1


@pytest.mark.parametrize("change", ["email", "suspended", "request_owner", "notification_owner", "sender", "flag"])
def test_pre_send_authorization_and_configuration_changes_cancel_old_job(client, db_session, catalog, monkeypatch, change):
    enable(monkeypatch)
    assert submit(client, catalog)[0].status_code == 201
    note = row(db_session)
    if change == "email":
        db_session.get(User, catalog.owner_id).email = "new@example.test"
    elif change == "suspended":
        db_session.get(User, catalog.owner_id).suspended_at = utc_now()
    elif change == "request_owner":
        db_session.get(CatalogRequest, note.request_id).user_id = catalog.other_id
    elif change == "notification_owner":
        note.user_id = catalog.other_id
    elif change == "sender":
        monkeypatch.setenv("MAIL_FROM", "new@example.test")
    else:
        monkeypatch.setenv("CATALOG_REQUEST_NOTIFICATIONS_ENABLED", "false")
    db_session.commit()
    assert notes.run_notification_job(*catalog.jobs[0])["status"] in {"cancelled", "disabled"}
    assert row(db_session).attempt_count == 0


def test_request_and_notification_rollback_together_on_failure(client, db_session, catalog, monkeypatch):
    from services import catalog_request_service
    original = notes.create_request_notification
    def fail(session, request):
        original(session, request)
        raise RuntimeError("test rollback")
    monkeypatch.setattr(notes, "create_request_notification", fail)
    with pytest.raises(RuntimeError, match="test rollback"):
        submit(client, catalog)
    db_session.expire_all()
    assert db_session.query(CatalogRequest).count() == 0
    assert db_session.query(Notification).count() == 0
    assert not catalog.jobs


def test_queue_error_cannot_fail_saved_public_request_and_recovery_is_bounded(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(notes, "_dispatch", lambda *args: (_ for _ in ()).throw(RuntimeError("private queue error")))
    assert submit(client, catalog)[0].status_code == 201
    note = row(db_session)
    assert note.status == "pending" and note.attempt_count == 0
    assert notes.recover_request_notifications()["considered"] == 0
    note.next_attempt_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    monkeypatch.setattr(notes, "_dispatch", lambda *args: catalog.jobs.append(args))
    assert notes.recover_request_notifications(limit=1000) == {"enqueued": 1, "considered": 1}
    assert len(catalog.jobs) == 1


@pytest.mark.parametrize("status,code", [("unknown", "request_timeout"), ("retryable", "rate_limited")])
def test_retry_preserves_exact_payload_key_and_obeys_retry_after(client, db_session, catalog, monkeypatch, status, code):
    enable(monkeypatch)
    submit(client, catalog)
    sent = fake_sender(monkeypatch, MailResult(status, code, retry_after_seconds=300))
    notes.run_notification_job(*catalog.jobs[0])
    note = row(db_session)
    assert note.status == "pending"
    assert note.next_attempt_at >= utc_now() + timedelta(seconds=295)
    assert notes.enqueue_request_notification(note.request_id) is False
    note.next_attempt_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert notes.recover_request_notifications()["enqueued"] == 1
    notes.run_notification_job(*catalog.jobs[-1])
    assert sent[0] == sent[1]
    assert row(db_session).attempt_count == 2


def test_ambiguous_crash_uses_same_key_and_stale_worker_cannot_claim_new_job(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    submit(client, catalog)
    old_job = catalog.jobs[0]
    def crashed(*args, **kwargs):
        raise RuntimeError("simulate accepted request then process crash")
    monkeypatch.setattr(ResendMailer, "send", crashed)
    with pytest.raises(RuntimeError):
        notes.run_notification_job(*old_job)
    note = row(db_session)
    old_key = note.idempotency_key
    note.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert notes.recover_request_notifications()["enqueued"] == 1
    assert notes.run_notification_job(*old_job)["status"] == "stale"
    sent = fake_sender(monkeypatch)
    assert notes.run_notification_job(*catalog.jobs[-1])["status"] == "accepted"
    assert sent[0][2] == old_key


@pytest.mark.parametrize("reason", ["window", "attempts", "long_retry"])
def test_unsafe_or_exhausted_retries_stop_without_new_key(client, db_session, catalog, monkeypatch, reason):
    enable(monkeypatch)
    submit(client, catalog)
    note = row(db_session)
    old_key = note.idempotency_key
    if reason == "window":
        note.first_attempt_at = utc_now() - timedelta(hours=23)
    elif reason == "attempts":
        note.attempt_count = notes.MAX_ATTEMPTS
    db_session.commit()
    if reason == "long_retry":
        sent = fake_sender(monkeypatch, MailResult("retryable", "rate_limited", retry_after_seconds=86400))
    outcome = notes.run_notification_job(*catalog.jobs[0])
    assert outcome["status"] == ("exhausted" if reason == "attempts" else "manual_review")
    assert row(db_session).idempotency_key == old_key
    assert notes.recover_request_notifications()["enqueued"] == 0


def test_operator_inspection_is_read_only_and_owner_scoped(app, client, db_session, catalog):
    submit(client, catalog)
    assert notes.notification_counts(catalog.owner_id) == {"disabled": 1}
    assert notes.notification_counts(catalog.other_id) == {}
    output = app.test_cli_runner().invoke(args=["catalog-notification-status", "--user-id", str(catalog.owner_id)])
    assert output.exit_code == 0
    data = json.loads(output.output)
    assert data["counts"] == {"disabled": 1}
    assert data["network_used"] is False and data["receipt_verified"] is False
    assert "owner@example" not in output.output and "Private buyer" not in output.output
    assert not catalog.jobs


@pytest.mark.parametrize("recipient", [None, "bad", "first@example.test,second@example.test", "bad\n@example.test"])
def test_invalid_owner_address_is_recorded_without_transport(client, db_session, catalog, monkeypatch, recipient):
    enable(monkeypatch)
    db_session.get(User, catalog.owner_id).email = recipient
    db_session.commit()
    assert submit(client, catalog)[0].status_code == 201
    note = row(db_session)
    assert note.status == "unconfigured" and note.result_code == "recipient_unconfigured"
    assert catalog.jobs == []


@pytest.mark.parametrize("change", ["missing_key", "smtp"])
def test_lost_api_configuration_is_terminal_without_smtp_fallback(client, db_session, catalog, monkeypatch, change):
    enable(monkeypatch)
    submit(client, catalog)
    if change == "missing_key":
        monkeypatch.delenv("RESEND_API_KEY")
    else:
        monkeypatch.setenv("MAIL_PROVIDER", "smtp")
    assert notes.run_notification_job(*catalog.jobs[0])["status"] == "unconfigured"
    assert row(db_session).attempt_count == 0
    enable(monkeypatch)
    assert notes.recover_request_notifications()["enqueued"] == 0


def test_queue_failures_stop_with_a_separate_dispatch_budget(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(notes, "_dispatch", lambda *args: (_ for _ in ()).throw(RuntimeError("queue offline")))
    submit(client, catalog)
    note = row(db_session)
    assert note.dispatch_attempt_count == 1 and note.attempt_count == 0
    note.dispatch_attempt_count = notes.MAX_DISPATCH_ATTEMPTS
    note.next_attempt_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert notes.recover_request_notifications() == {"enqueued": 0, "considered": 1}
    note = row(db_session)
    assert note.status == "exhausted" and note.result_code == "dispatch_limit"
    assert note.attempt_count == 0


def test_ambiguous_history_at_dispatch_limit_requires_manual_review(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    submit(client, catalog)
    note = row(db_session)
    note.status = "pending"
    note.first_attempt_at = utc_now()
    note.dispatch_attempt_count = notes.MAX_DISPATCH_ATTEMPTS
    note.next_attempt_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert notes.recover_request_notifications()["enqueued"] == 0
    assert row(db_session).status == "manual_review"


@pytest.mark.parametrize("lease", ["missing", "active", "expired"])
def test_recovery_claims_only_abandoned_jobs_with_a_fresh_token(client, db_session, catalog, monkeypatch, lease):
    enable(monkeypatch)
    submit(client, catalog)
    note = row(db_session)
    old_token = note.claim_token
    if lease == "missing":
        note.lease_expires_at = None
    elif lease == "expired":
        note.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    count = 0 if lease == "active" else 1
    assert notes.recover_request_notifications() == {"enqueued": count, "considered": count}
    current_token = row(db_session).claim_token
    assert (current_token == old_token) == (lease == "active")


def test_owner_admission_wait_does_not_consume_retry_budget(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(notes, "ACTIVE_OWNER_LIMIT", 1)
    submit(client, catalog)
    submit(client, catalog)
    db_session.expire_all()
    all_notes = db_session.query(Notification).order_by(Notification.id).all()
    assert [note.status for note in all_notes] == ["queued", "pending"]
    assert [note.dispatch_attempt_count for note in all_notes] == [1, 0]
    assert all_notes[1].next_attempt_at > utc_now()
    assert len(catalog.jobs) == 1


@pytest.mark.parametrize("change", ["email", "suspended", "request_owner", "notification_owner", "recipient"])
def test_authorization_drift_at_atomic_claim_prevents_old_or_foreign_delivery(client, db_session, catalog, monkeypatch, change):
    from sqlalchemy import update
    enable(monkeypatch)
    submit(client, catalog)
    original = notes._owner_snapshot
    def drift(session, notification):
        verified = original(session, notification)
        if change == "email":
            session.execute(update(User).where(User.id == catalog.owner_id).values(email="new@example.test"))
        elif change == "suspended":
            session.execute(update(User).where(User.id == catalog.owner_id).values(suspended_at=utc_now()))
        elif change == "request_owner":
            session.execute(update(CatalogRequest).where(CatalogRequest.id == notification.request_id).values(user_id=catalog.other_id))
        elif change == "notification_owner":
            session.execute(update(Notification).where(Notification.id == notification.id).values(user_id=catalog.other_id))
        else:
            session.execute(update(Notification).where(Notification.id == notification.id).values(recipient="foreign@example.test"))
        return verified
    monkeypatch.setattr(notes, "_owner_snapshot", drift)
    assert notes.run_notification_job(*catalog.jobs[0])["status"] == "cancelled"
    assert row(db_session).attempt_count == 0


def test_live_queue_wait_can_exceed_dispatch_budget_and_still_send_once(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    submit(client, catalog)
    original_job = catalog.jobs[0]
    monkeypatch.setattr(notes, "_queue_presence", lambda *args: "alive")
    for _ in range(notes.MAX_DISPATCH_ATTEMPTS + 2):
        note = row(db_session)
        note.lease_expires_at = utc_now() - timedelta(seconds=1)
        db_session.commit()
        assert notes.recover_request_notifications() == {"enqueued": 0, "considered": 1}
    note = row(db_session)
    assert note.status == "queued" and note.dispatch_attempt_count == 1 and note.attempt_count == 0
    assert catalog.jobs == [original_job]
    # A legitimate queued job may start after the reconciliation hint expired.
    note.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    sent = fake_sender(monkeypatch)
    assert notes.run_notification_job(*original_job)["status"] == "accepted"
    assert len(sent) == 1


def test_redis_uncertainty_never_rotates_claim_or_creates_duplicate(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    submit(client, catalog)
    original_job = catalog.jobs[0]
    note = row(db_session)
    note.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    monkeypatch.setattr(notes, "_queue_presence", lambda *args: "unknown")
    assert notes.recover_request_notifications() == {"enqueued": 0, "considered": 1}
    note = row(db_session)
    assert note.status == "queued" and note.claim_token == original_job[1]
    assert note.dispatch_attempt_count == 1 and note.attempt_count == 0
    assert catalog.jobs == [original_job]


def test_lost_enqueue_response_preserves_existing_live_job(client, db_session, catalog, monkeypatch):
    enable(monkeypatch)
    def ambiguous_dispatch(*args):
        catalog.jobs.append(args)
        raise RuntimeError("enqueue accepted but reply lost")
    monkeypatch.setattr(notes, "_dispatch", ambiguous_dispatch)
    submit(client, catalog)
    note = row(db_session)
    assert note.status == "pending" and len(catalog.jobs) == 1
    note.next_attempt_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    monkeypatch.setattr(notes, "_queue_presence", lambda *args: "alive")
    assert notes.recover_request_notifications() == {"enqueued": 0, "considered": 1}
    assert row(db_session).status == "queued" and len(catalog.jobs) == 1


@pytest.mark.parametrize("presence", ["alive", "unknown"])
def test_expired_or_uncertain_queued_job_retains_owner_capacity(client, db_session, catalog, monkeypatch, presence):
    enable(monkeypatch)
    monkeypatch.setattr(notes, "ACTIVE_OWNER_LIMIT", 1)
    submit(client, catalog)
    note = row(db_session)
    note.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    monkeypatch.setattr(notes, "_queue_presence", lambda *args: presence)
    # New requests cannot use an expired hint to bypass queued-job admission.
    submit(client, catalog)
    db_session.expire_all()
    assert [item.status for item in db_session.query(Notification).order_by(Notification.id)] == ["queued", "pending"]
    assert len(catalog.jobs) == 1


@pytest.mark.parametrize("queue_status,expected", [
    ("queued", "alive"), ("deferred", "alive"), ("scheduled", "alive"),
    ("started", "alive"), ("failed", "gone"), (None, "unknown"),
])
def test_rq_presence_inspection_checks_same_job_and_closes_connection(monkeypatch, queue_status, expected):
    from redis import Redis
    from rq.job import Job
    closed = []
    connection = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(notes, "resolve_queue_backend_name", lambda: "rq")
    monkeypatch.setattr(notes, "resolve_media_queue_name", lambda: "media")
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: connection)
    job = SimpleNamespace(origin="media", args=(3, "claim"), get_status=lambda **kwargs: queue_status)
    monkeypatch.setattr(Job, "fetch", lambda *args, **kwargs: job)
    assert QUEUE_PRESENCE_IMPLEMENTATION(3, "claim") == expected
    assert closed == [True]


@pytest.mark.parametrize("failure", ["missing", "redis_unavailable", "foreign_job"])
def test_rq_presence_uncertainty_is_distinct_from_missing_job(monkeypatch, failure):
    from redis import Redis
    from rq.exceptions import NoSuchJobError
    from rq.job import Job
    monkeypatch.setattr(notes, "resolve_queue_backend_name", lambda: "rq")
    monkeypatch.setattr(notes, "resolve_media_queue_name", lambda: "media")
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    def fetch(*args, **kwargs):
        if failure == "missing":
            raise NoSuchJobError()
        if failure == "redis_unavailable":
            raise RuntimeError("private Redis detail")
        return SimpleNamespace(origin="foreign", args=(999, "claim"))
    monkeypatch.setattr(Job, "fetch", fetch)
    assert QUEUE_PRESENCE_IMPLEMENTATION(3, "claim") == ("gone" if failure == "missing" else "unknown")


def test_0025_upgrade_preserves_requests_and_does_not_backfill(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text
    url = f"sqlite:///{tmp_path / 'notification_migration.db'}"
    config = Config("alembic.ini")
    config.attributes["configured_sqlalchemy_url"] = url
    config.attributes["skip_logging_config"] = True
    command.upgrade(config, "20260930_0025")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            # The baseline creates current metadata. Reproduce an actual 0025
            # installation lacking the later table before upgrading.
            connection.execute(text("DROP TABLE IF EXISTS catalog_request_notifications"))
            connection.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'synthetic', 'unused')"))
            connection.execute(text("INSERT INTO price_lists (id, user_id, name, token, created_at) VALUES (1, 1, 'synthetic', 'synthetic-token', CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO catalog_requests (id, user_id, pricelist_id, pricelist_name, reference, submission_key, payload_hash, buyer_instagram, created_at) VALUES (1, 1, 1, 'synthetic', '0123456789abcdefabcd', 'synthetic-key', 'synthetic', 'synthetic', CURRENT_TIMESTAMP)"))
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260930_0026"
            assert connection.execute(text("SELECT count(*) FROM catalog_requests")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM catalog_request_notifications")).scalar_one() == 0
        columns = {column["name"]: column for column in inspect(engine).get_columns("catalog_request_notifications")}
        assert columns["dispatch_attempt_count"]["nullable"] is False
        assert columns["attempt_count"]["nullable"] is False
        assert any(set(item["column_names"]) == {"request_id", "notification_type"}
                   for item in inspect(engine).get_unique_constraints("catalog_request_notifications"))
    finally:
        engine.dispose()
