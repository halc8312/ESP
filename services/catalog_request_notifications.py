"""One opt-in owner notification per request; no historical auto-backfill.

API acceptance is never inbox-delivery evidence. Retries preserve the frozen
sender, recipient and body, and stop before the provider's 24-hour key expiry.
"""
from __future__ import annotations

from datetime import timedelta
import logging
import os
import re
import uuid

from sqlalchemy import and_, func, or_

from database import create_isolated_session
from models import CatalogRequest, User
from models_mail import CatalogRequestNotification as Notification
from services.mail_service import MailMessage, MailSettings, ResendMailer, validate_message
from services.media_queue import enqueue_media_job, resolve_queue_backend_name, resolve_media_queue_name, resolve_redis_url
from time_utils import utc_now

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
MAX_ATTEMPTS = 8
MAX_DISPATCH_ATTEMPTS = 16
RETRY_WINDOW = timedelta(hours=23)
ACTIVE_OWNER_LIMIT = 10
ACTIVE_GLOBAL_LIMIT = 50
TERMINAL = frozenset({"accepted", "disabled", "unconfigured", "rejected", "cancelled", "manual_review", "exhausted"})


def normalized_address(value):
    value = str(value or "").strip()
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    value = local + "@" + domain.lower()
    return value if not validate_message(MailMessage(value, "ESP", "ESP"), "validate") else None


def configuration_status():
    dedicated = os.environ.get("CATALOG_REQUEST_NOTIFICATIONS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    mail = ResendMailer().configuration_status()
    provider = os.environ.get("MAIL_PROVIDER", "").strip().lower() == "resend"
    status = "disabled" if not dedicated or mail["status"] == "disabled" else (
        "ready" if provider and mail["status"] == "ready" else "unconfigured"
    )
    return {"status": status, "notifications_enabled": dedicated,
            "mail_enabled": mail["enabled"], "provider_configured": provider,
            "key_configured": mail["key_configured"], "sender_valid": mail["sender_valid"],
            "network_used": False, "receipt_verified": False}


def create_request_notification(session, catalog_request):
    """Called before the request commit, in the same transaction."""
    session.flush()
    owner = session.query(User).filter_by(id=catalog_request.user_id).first()
    recipient = normalized_address(owner.email) if owner else None
    settings = MailSettings.from_env()
    config = configuration_status()
    status = config["status"]
    code = "notifications_disabled" if status == "disabled" else "mail_unconfigured" if status != "ready" else None
    if status == "ready" and (owner is None or owner.suspended_at is not None or not recipient):
        status, code = "unconfigured", "recipient_unconfigured"
    reference = catalog_request.reference if re.fullmatch(r"[a-f0-9]{20}", catalog_request.reference or "") else ""
    row = Notification(
        request_id=catalog_request.id, user_id=catalog_request.user_id,
        notification_type="request_created", recipient=recipient, sender=settings.sender,
        subject="ESP 新しい商品依頼",
        body="ESPに新しい商品依頼が届きました。\n" + (f"受付番号: {reference}\n" if reference else "")
             + "ESPへログインし、依頼一覧で内容をご確認ください。\n",
        idempotency_key=f"esp-catalog-request-v1/{catalog_request.id}",
        status="pending" if status == "ready" else status, result_code=code,
        next_attempt_at=utc_now() if status == "ready" else None,
    )
    session.add(row)
    return row


def _eligible_owner(session, row):
    owner = session.query(User).filter_by(id=row.user_id, suspended_at=None).first()
    request = session.query(CatalogRequest.id).filter_by(id=row.request_id, user_id=row.user_id).first()
    return owner is not None and request is not None and normalized_address(owner.email) == row.recipient and row.recipient is not None


def _lock_dispatch_admission(session):
    # Match the global admission lock and always take it before domain locks.
    first_user = session.query(User.id).order_by(User.id).first()
    if first_user:
        session.query(User).filter(User.id == first_user[0]).with_for_update().first()
        session.query(User).filter(User.id == first_user[0]).update({User.id: User.id}, synchronize_session=False)


def _owner_snapshot(session, row):
    """Lock owner then request; return the exact email guarded at handoff."""
    owner = session.query(User).filter_by(id=row.user_id).populate_existing().with_for_update().first()
    request = session.query(CatalogRequest).filter_by(id=row.request_id).populate_existing().with_for_update().first()
    if (owner is None or owner.suspended_at is not None or request is None
            or request.user_id != row.user_id or row.recipient is None
            or normalized_address(owner.email) != row.recipient):
        return None
    return owner.email


def _queue_presence(notification_id, token):
    """Read Redis outside a DB transaction; uncertainty never creates a job."""
    if resolve_queue_backend_name() != "rq":
        return "unknown"
    try:
        from redis import Redis
        from rq.exceptions import NoSuchJobError
        from rq.job import Job
        connection = Redis.from_url(resolve_redis_url(), socket_connect_timeout=1, socket_timeout=1)
        try:
            job = Job.fetch(f"catalog-mail-{notification_id}-{token}", connection=connection)
            if job.origin != resolve_media_queue_name() or tuple(job.args) != (notification_id, token):
                return "unknown"
            status = job.get_status(refresh=True)
            status = status.value if hasattr(status, "value") else status
            if status in {"queued", "deferred", "scheduled", "started"}:
                return "alive"
            return "gone" if status in {"finished", "failed", "stopped", "canceled"} else "unknown"
        except NoSuchJobError:
            return "gone"
        finally:
            connection.close()
    except Exception as error:
        logger.warning("Notification queue inspection unavailable (%s)", type(error).__name__)
        return "unknown"


def _dispatch(notification_id, token):
    # Local development never silently sends synchronously from a public POST.
    if resolve_queue_backend_name() != "rq":
        raise RuntimeError("notification_queue_unconfigured")
    enqueue_media_job(job_id=f"catalog-mail-{notification_id}-{token}",
                      func="services.catalog_request_notifications.run_notification_job",
                      args=(notification_id, token), timeout_seconds=60,
                      description="ESP owner request notification")


def enqueue_request_notification(request_id):
    session = create_isolated_session()
    dispatch = None
    observed_token = presence = None
    try:
        observed = session.query(Notification).filter_by(request_id=request_id, notification_type="request_created").first()
        now = utc_now()
        if observed is None or observed.status in TERMINAL:
            return False
        if observed is not None and observed.status not in TERMINAL and observed.claim_token and (
            observed.status == "pending" or not observed.lease_expires_at or observed.lease_expires_at <= now
        ):
            observed_token, observed_id = observed.claim_token, observed.id
    finally:
        session.close()
    if observed_token:
        presence = _queue_presence(observed_id, observed_token)
    session = create_isolated_session()
    try:
        _lock_dispatch_admission(session)
        row = session.query(Notification).filter_by(request_id=request_id, notification_type="request_created").with_for_update().first()
        now = utc_now()
        if row is None or row.status in TERMINAL:
            return False
        if row.status in {"queued", "running"} and row.lease_expires_at and row.lease_expires_at > now:
            return False
        if row.next_attempt_at and row.next_attempt_at > now:
            return False
        if observed_token and row.claim_token != observed_token:
            return False
        config = configuration_status()
        if config["status"] != "ready":
            row.status, row.result_code = config["status"], "notifications_disabled" if config["status"] == "disabled" else "mail_unconfigured"
        elif not _eligible_owner(session, row):
            row.status, row.result_code = "cancelled", "recipient_changed_or_inactive"
        elif MailSettings.from_env().sender != row.sender:
            row.status, row.result_code = "cancelled", "sender_changed"
        elif presence == "unknown":
            # Retain the same claim and slot, but move this inspection behind
            # other overdue rows so a partial Redis failure cannot starve them.
            row.lease_expires_at, row.updated_at = now + timedelta(seconds=LEASE_SECONDS), now
            if row.status == "pending":
                row.next_attempt_at = now + timedelta(seconds=30)
            session.commit()
            return False
        elif presence == "alive":
            # Queue latency does not consume dispatch attempts or rotate the
            # claim. A job can validly start after this reconciliation hint.
            row.status = "running" if row.status == "running" else "queued"
            row.lease_expires_at, row.next_attempt_at, row.updated_at = now + timedelta(seconds=LEASE_SECONDS), None, now
        elif row.first_attempt_at and now >= row.first_attempt_at + RETRY_WINDOW:
            row.status, row.result_code = "manual_review", "retry_window_expired"
        elif row.attempt_count >= MAX_ATTEMPTS:
            row.status, row.result_code = "exhausted", "retry_limit"
        elif row.dispatch_attempt_count >= MAX_DISPATCH_ATTEMPTS:
            row.status, row.result_code = ("manual_review" if row.first_attempt_at else "exhausted"), "dispatch_limit"
        else:
            # Serialize dispatch admission across PostgreSQL processes.
            # An expired hint does not prove a queued RQ job disappeared. Keep
            # its slot (including a lost enqueue response) until reconciliation.
            active = session.query(Notification).filter(Notification.id != row.id, or_(
                Notification.status.in_(("queued", "running")),
                and_(Notification.status == "pending", Notification.claim_token.is_not(None), Notification.result_code == "queue_unavailable"),
            ))
            if active.count() >= ACTIVE_GLOBAL_LIMIT or active.filter(Notification.user_id == row.user_id).count() >= ACTIVE_OWNER_LIMIT:
                row.status, row.next_attempt_at = "pending", now + timedelta(seconds=30)
            else:
                token = uuid.uuid4().hex
                updated = session.query(Notification).filter(Notification.id == row.id, Notification.claim_token == row.claim_token, Notification.status == row.status).update({
                    Notification.status: "queued", Notification.claim_token: token,
                    Notification.lease_expires_at: now + timedelta(seconds=LEASE_SECONDS),
                    Notification.updated_at: now, Notification.next_attempt_at: None,
                    Notification.dispatch_attempt_count: row.dispatch_attempt_count + 1,
                }, synchronize_session=False)
                if updated:
                    dispatch = (row.id, token)
        session.commit()
    except Exception as error:
        session.rollback()
        logger.warning("Notification admission failed (%s)", type(error).__name__)
    finally:
        session.close()
    if dispatch is None:
        return False
    try:
        _dispatch(*dispatch)
        return True
    except Exception as error:
        session = create_isolated_session()
        try:
            session.query(Notification).filter_by(id=dispatch[0], status="queued", claim_token=dispatch[1]).update({
                Notification.status: "pending", Notification.result_code: "queue_unavailable",
                Notification.lease_expires_at: None, Notification.next_attempt_at: utc_now() + timedelta(seconds=30),
            }, synchronize_session=False)
            session.commit()
        finally:
            session.close()
        logger.warning("Notification dispatch failed (%s)", type(error).__name__)
        return False


def run_notification_job(notification_id, token):
    session = create_isolated_session()
    message = settings = key = None
    try:
        now = utc_now()
        _lock_dispatch_admission(session)
        row = session.query(Notification).filter_by(id=notification_id, status="queued", claim_token=token).first()
        if row is None:
            return {"status": "stale"}
        frozen = (row.user_id, row.request_id, row.recipient, row.sender, row.subject, row.body, row.idempotency_key)
        email_snapshot = _owner_snapshot(session, row)
        row = session.query(Notification).filter_by(id=notification_id, status="queued", claim_token=token).populate_existing().with_for_update().first()
        if row is None:
            return {"status": "stale"}
        config = configuration_status()
        if config["status"] != "ready":
            row.status, row.result_code = config["status"], "notifications_disabled" if config["status"] == "disabled" else "mail_unconfigured"
        elif email_snapshot is None or frozen != (row.user_id, row.request_id, row.recipient, row.sender, row.subject, row.body, row.idempotency_key):
            row.status, row.result_code = "cancelled", "recipient_changed_or_inactive"
        elif MailSettings.from_env().sender != row.sender:
            row.status, row.result_code = "cancelled", "sender_changed"
        elif row.first_attempt_at and now >= row.first_attempt_at + RETRY_WINDOW:
            row.status, row.result_code = "manual_review", "retry_window_expired"
        elif row.attempt_count >= MAX_ATTEMPTS:
            row.status, row.result_code = "exhausted", "retry_limit"
        else:
            owner_guard = session.query(User.id).filter(User.id == row.user_id, User.email == email_snapshot, User.suspended_at.is_(None)).exists()
            request_guard = session.query(CatalogRequest.id).filter(CatalogRequest.id == row.request_id, CatalogRequest.user_id == row.user_id).exists()
            frozen_guard = and_(Notification.user_id == frozen[0], Notification.request_id == frozen[1],
                                Notification.recipient == frozen[2], Notification.sender == frozen[3],
                                Notification.subject == frozen[4], Notification.body == frozen[5],
                                Notification.idempotency_key == frozen[6])
            # This conditional update is the immutable send handoff. Owner and
            # request locks are released at commit before the bounded HTTP call.
            claimed = session.query(Notification).filter_by(id=row.id, status="queued", claim_token=token).filter(owner_guard, request_guard, frozen_guard).update({
                Notification.status: "running", Notification.attempt_count: row.attempt_count + 1,
                Notification.first_attempt_at: row.first_attempt_at or now,
                Notification.last_attempt_at: now, Notification.updated_at: now,
                Notification.lease_expires_at: now + timedelta(seconds=LEASE_SECONDS),
            }, synchronize_session=False)
            if not claimed:
                session.query(Notification).filter_by(id=row.id, status="queued", claim_token=token).update({
                    Notification.status: "cancelled", Notification.result_code: "recipient_changed_or_inactive",
                    Notification.lease_expires_at: None, Notification.updated_at: now,
                }, synchronize_session=False)
                session.commit()
                return {"status": "cancelled"}
            live_settings = MailSettings.from_env()
            settings = MailSettings(api_key=live_settings.api_key, sender=row.sender, enabled=True)
            message, key = MailMessage(row.recipient, row.subject, row.body), row.idempotency_key
        session.commit()
        if message is None:
            return {"status": row.status}
    finally:
        session.close()
    result = ResendMailer(settings).send(message, idempotency_key=key)
    session = create_isolated_session()
    try:
        row = session.query(Notification).filter_by(id=notification_id, status="running", claim_token=token).with_for_update().first()
        if row is None:
            return {"status": "stale"}
        now = utc_now()
        row.result_code = result.code
        if result.status == "accepted":
            row.status, row.message_id = "accepted", result.message_id
        elif result.status in {"retryable", "unknown"}:
            delay = max(min(3600, 30 * 2 ** min(row.attempt_count, 7)), result.retry_after_seconds or 0)
            if now + timedelta(seconds=min(delay, 86400)) >= row.first_attempt_at + RETRY_WINDOW:
                row.status, row.result_code = "manual_review", "retry_window_expired"
            elif row.attempt_count >= MAX_ATTEMPTS:
                row.status, row.result_code = "exhausted", "retry_limit"
            else:
                row.status, row.next_attempt_at = "pending", now + timedelta(seconds=delay)
        else:
            row.status = "rejected"
        row.lease_expires_at, row.updated_at = None, now
        session.commit()
        return {"status": row.status, "receipt_verified": False}
    finally:
        session.close()


def recover_request_notifications(limit=20):
    session = create_isolated_session()
    try:
        now = utc_now()
        ids = [row[0] for row in session.query(Notification.request_id).filter(or_(
            and_(Notification.status == "pending", or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= now)),
            and_(Notification.status.in_(("queued", "running")), or_(Notification.lease_expires_at.is_(None), Notification.lease_expires_at <= now)),
        )).order_by(func.coalesce(Notification.next_attempt_at, Notification.lease_expires_at, Notification.created_at), Notification.id).limit(min(20, max(1, int(limit)))).all()]
    finally:
        session.close()
    return {"enqueued": sum(enqueue_request_notification(request_id) for request_id in ids), "considered": len(ids)}


def notification_counts(user_id=None):
    session = create_isolated_session()
    try:
        query = session.query(Notification.status, func.count(Notification.id))
        if user_id is not None:
            query = query.filter(Notification.user_id == user_id)
        return dict(query.group_by(Notification.status).all())
    finally:
        session.close()
