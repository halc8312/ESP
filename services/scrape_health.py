"""Passive, durable scraper observations. This module never fetches a marketplace.

Only operational site/route aggregates are retained, not user identifiers, URLs,
keywords, titles, cookies, or exception text. Alerts are an at-least-once outbox:
a crash after HTTP acceptance and before commit can cause a duplicate delivery.
``delivered`` means HTTP accepted, never confirmed human receipt.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, timedelta

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from database import create_isolated_session
from models import ScrapeHealthDelivery, ScrapeHealthObservation, ScrapeHealthState
from services.alerts import get_alert_dispatcher
from time_utils import utc_now

logger = logging.getLogger(__name__)
SITES = ("mercari", "yahoo", "rakuma", "surugaya", "offmall", "yahuoku", "snkrdunk", "recordcity")
ROUTES = ("search", "detail", "patrol")
REASONS = frozenset({
    "invalid_url", "unsupported_route", "access_blocked", "captcha", "rate_limited",
    "fetch_error", "invalid_result", "missing_price", "unknown_status",
    "persistence_error", "inconclusive", "empty_result", "timeout",
    "configuration_error", "unknown",
})
TERMINAL_DELIVERY_STATUSES = ("delivered", "superseded", "exhausted", "expired")
DISPATCH_STATUSES = ("delivered", "unconfigured", "cooldown", "rate_limited", "in_flight", "failed")
STALE_AFTER = timedelta(hours=24)
DELIVERY_LIFETIME = timedelta(days=7)
MAX_FAILURE_ATTEMPTS = 8
HISTORY_PER_ROUTE = 200
DELIVERIES_PER_ROUTE = 100


def _safe_error_type(exc):
    token = type(exc).__name__
    return token if re.fullmatch(r"[A-Za-z0-9_]{1,80}", token) else "unknown"


def _iso(value):
    return value.replace(tzinfo=UTC).isoformat() if value else None


def _count(value):
    return min(1_000_000, max(0, int(value)))


def _prune_route(session, site, route):
    for model, maximum in ((ScrapeHealthObservation, HISTORY_PER_ROUTE), (ScrapeHealthDelivery, DELIVERIES_PER_ROUTE)):
        query = session.query(model.id).filter_by(site=site, route=route)
        if model is ScrapeHealthDelivery:
            query = query.filter(model.status.in_(TERMINAL_DELIVERY_STATUSES))
        ids = [row[0] for row in query.order_by(model.id.desc()).offset(maximum).limit(1000).all()]
        if ids:
            session.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)


def _enqueue(session, state, event_type, now):
    # Obsolete unsent incident/recovery messages must not arrive out of order.
    # An active network claim is fenced separately and cannot be un-sent.
    session.query(ScrapeHealthDelivery).filter(
        ScrapeHealthDelivery.site == state.site,
        ScrapeHealthDelivery.route == state.route,
        ~ScrapeHealthDelivery.status.in_(TERMINAL_DELIVERY_STATUSES),
        or_(ScrapeHealthDelivery.claim_token.is_(None), ScrapeHealthDelivery.lease_expires_at <= now),
    ).update({"status": "superseded", "claim_token": None, "lease_expires_at": None}, synchronize_session=False)
    session.add(ScrapeHealthDelivery(
        site=state.site, route=state.route, incident_number=state.incident_number,
        event_type=event_type, reason=state.reason, status="pending",
        created_at=now, next_attempt_at=now,
    ))


def record_scrape_observation(*, site, route, outcome, reason=None, success_count=0, error_count=0):
    """Record one completed job/batch; telemetry failure never fails that job.

    A mixed batch is a failure observation but still advances last_success_at
    when it actually contains successful products. Only an all-success outcome
    closes an incident. Empty results do not clear prior failure evidence.
    """
    try:
        if site not in SITES or route not in ROUTES or outcome not in ("success", "failure", "no_observations"):
            raise ValueError("unsupported observation")
        success_count, error_count = _count(success_count), _count(error_count)
        if error_count:
            outcome = "failure"
        elif outcome == "success" and not success_count:
            outcome = "no_observations"
        if outcome == "failure":
            error_count = max(1, error_count)
        reason = reason if reason in REASONS else ("unknown" if reason else None)
        for attempt in range(3):
            session = create_isolated_session()
            try:
                now = utc_now()
                state = session.query(ScrapeHealthState).filter_by(site=site, route=route).with_for_update().first()
                if state is None:
                    state = ScrapeHealthState(site=site, route=route, consecutive_failures=0, incident_open=False, incident_number=0)
                    session.add(state)
                state.last_observed_at = now
                state.last_outcome = outcome
                if success_count:
                    state.last_success_at = now
                if outcome == "failure":
                    state.reason = reason or "unknown"
                    state.last_failure_at = now
                    state.consecutive_failures += 1
                    if state.consecutive_failures >= 2 and not state.incident_open:
                        state.incident_open = True
                        state.incident_number += 1
                        _enqueue(session, state, "incident", now)
                elif outcome == "success":
                    state.reason = None
                    state.consecutive_failures = 0
                    if state.incident_open:
                        state.incident_open = False
                        _enqueue(session, state, "recovery", now)
                    else:
                        # A single intervening failure can defer an unsent
                        # same-generation recovery without opening incident2.
                        # Fresh actual success makes that existing event due;
                        # accepted deliveries are never rearmed.
                        session.query(ScrapeHealthDelivery).filter_by(
                            site=site, route=route, incident_number=state.incident_number,
                            event_type="recovery", status="deferred",
                        ).update({"status": "pending", "next_attempt_at": now}, synchronize_session=False)
                elif not state.consecutive_failures:
                    state.reason = reason or "empty_result"
                session.add(ScrapeHealthObservation(
                    site=site, route=route, observed_at=now, outcome=outcome,
                    reason=reason, success_count=success_count, error_count=error_count,
                ))
                session.flush()
                _prune_route(session, site, route)
                session.commit()
                return True
            except (IntegrityError, OperationalError, StaleDataError):
                session.rollback()
                if attempt == 2:
                    raise
            finally:
                session.close()
    except Exception as exc:
        logger.warning("Scrape health observation was not persisted error_type=%s", _safe_error_type(exc))
        return False


def _status(state, now):
    if state is None:
        return "unobserved"
    evidence = [value for value in (state.last_success_at, state.last_failure_at) if value]
    if evidence and now - max(evidence) > STALE_AFTER:
        return "stale"
    if state.incident_open:
        return "failed"
    if state.consecutive_failures:
        return "degraded"
    if state.last_outcome == "no_observations":
        return "no_observations"
    return "healthy" if state.last_success_at else "unobserved"


def list_scrape_health():
    """Admin-only projection; a database error is explicit, never false green."""
    now = utc_now()
    # This process's configuration only, not evidence of worker configuration
    # or delivery. A broken config diagnostic must not hide DB observations.
    try:
        configured = getattr(get_alert_dispatcher(), "scrape_webhook_configured", None)
        scrape_alert_configured = configured if type(configured) is bool else None
    except Exception as exc:
        scrape_alert_configured = None
        logger.warning("Scrape alert configuration unavailable error_type=%s", _safe_error_type(exc))
    states, deliveries, available = {}, {}, True
    try:
        with create_isolated_session() as session:
            states = {(row.site, row.route): row for row in session.query(ScrapeHealthState).all()}
            for row in session.query(ScrapeHealthDelivery).order_by(ScrapeHealthDelivery.id.desc()).all():
                deliveries.setdefault((row.site, row.route), row)
            # Scalar values remain usable after closing this read-only session.
    except Exception as exc:
        available = False
        states, deliveries = {}, {}
        logger.warning("Scrape health read unavailable error_type=%s", _safe_error_type(exc))
    rows = []
    for site in SITES:
        for route in ROUTES:
            state, delivery = states.get((site, route)), deliveries.get((site, route))
            rows.append({
                "site": site, "route": route,
                "status": _status(state, now) if available else "monitoring_unavailable",
                "monitoring_available": available,
                "scrape_alert_configured": scrape_alert_configured,
                "last_observed_at": _iso(state.last_observed_at) if state else None,
                "last_success_at": _iso(state.last_success_at) if state else None,
                "last_failure_at": _iso(state.last_failure_at) if state else None,
                "last_outcome": state.last_outcome if state else None,
                "consecutive_failures": state.consecutive_failures if state else 0,
                "incident_open": state.incident_open if state else False,
                "reason": state.reason if state else None,
                "latest_delivery_status": delivery.status if delivery else None,
                "latest_delivery_at": _iso(delivery.delivered_at) if delivery else None,
                "latest_delivery_attempt_at": _iso(delivery.last_attempt_at) if delivery else None,
                "latest_delivery_attempt_count": delivery.attempt_count if delivery else 0,
            })
    return rows


def _event_disposition(state, event):
    """Distinguish obsolete events from temporarily uncertain recovery."""
    if state is None or state.incident_number != event["incident_number"]:
        return "obsolete"
    if event["event_type"] == "incident":
        return "relevant" if state.incident_open else "obsolete"
    if state.incident_open:
        return "obsolete"
    return "deferred" if state.consecutive_failures else "relevant"


def _locked_state(session, event):
    # Use the same state -> delivery lock order as observation recording.
    return session.query(ScrapeHealthState).filter_by(
        site=event["site"], route=event["route"],
    ).with_for_update().first()


def _claim_delivery(delivery_id, now):
    token = uuid.uuid4().hex
    with create_isolated_session() as session:
        row = session.query(ScrapeHealthDelivery).filter_by(id=delivery_id).first()
        if row is None:
            return None
        claim = {"id": row.id, "token": token, "site": row.site, "route": row.route,
                 "incident_number": row.incident_number, "event_type": row.event_type,
                 "reason": row.reason, "failure_count": row.failure_count}
        state = _locked_state(session, claim)
        eligible = session.query(ScrapeHealthDelivery).filter(
            ScrapeHealthDelivery.id == delivery_id,
            ~ScrapeHealthDelivery.status.in_(TERMINAL_DELIVERY_STATUSES),
            ScrapeHealthDelivery.next_attempt_at <= now,
            or_(ScrapeHealthDelivery.lease_expires_at.is_(None), ScrapeHealthDelivery.lease_expires_at <= now),
        )
        disposition = _event_disposition(state, claim)
        if disposition != "relevant":
            eligible.update({
                "status": "deferred" if disposition == "deferred" else "superseded",
                "next_attempt_at": now + timedelta(minutes=5),
                "claim_token": None, "lease_expires_at": None,
            }, synchronize_session=False)
            session.commit()
            return None
        updated = eligible.update({
            "claim_token": token, "lease_expires_at": now + timedelta(minutes=2),
            "status": "in_flight", "last_attempt_at": now,
            "attempt_count": ScrapeHealthDelivery.attempt_count + 1,
        }, synchronize_session=False)
        if not updated:
            return None
        session.commit()
        return claim


def _claim_disposition(claim):
    with create_isolated_session() as session:
        state = _locked_state(session, claim)
        owned = session.query(ScrapeHealthDelivery).filter_by(
            id=claim["id"], claim_token=claim["token"], status="in_flight",
        )
        if owned.first() is None:
            return "obsolete"
        disposition = _event_disposition(state, claim)
        if disposition != "relevant":
            owned.update({
                "status": "deferred" if disposition == "deferred" else "superseded",
                "next_attempt_at": utc_now() + timedelta(minutes=5),
                "claim_token": None, "lease_expires_at": None,
            }, synchronize_session=False)
            session.commit()
        return disposition


def _deliver(claim):
    # Evidence may have changed after claiming but before the HTTP attempt.
    disposition = _claim_disposition(claim)
    if disposition != "relevant":
        return "deferred" if disposition == "deferred" else "superseded"
    try:
        result = get_alert_dispatcher().notify_scrape_issue_result(
            event_type=f"scrape_health_{claim['event_type']}", site=claim["site"],
            page_type=claim["route"], field="aggregate",
            severity="info" if claim["event_type"] == "recovery" else "warning",
            message="Passive observation recovered" if claim["event_type"] == "recovery" else "Repeated passive observation failures",
            details={"reason": claim["reason"], "incident_number": claim["incident_number"]},
            dedupe_key=f"scrape_health:{claim['site']}:{claim['route']}:{claim['incident_number']}:{claim['event_type']}",
        )
        status = result.get("status")
        if status not in DISPATCH_STATUSES:
            raise ValueError("unsupported delivery disposition")
        error_type = result.get("error_type")
        if error_type and not re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(error_type)):
            error_type = "unknown"
    except Exception as exc:
        status, error_type = "failed", _safe_error_type(exc)
    now = utc_now()
    failures = claim["failure_count"] + (status == "failed")
    delay_minutes = 60 if status == "unconfigured" else min(60, 5 * (2 ** min(failures, 4)))
    stored_status = "exhausted" if failures >= MAX_FAILURE_ATTEMPTS else status
    with create_isolated_session() as session:
        state = _locked_state(session, claim)
        # In-flight HTTP cannot be un-sent. Retain genuine HTTP acceptance,
        # but never schedule obsolete failed/suppressed events for retry.
        disposition = _event_disposition(state, claim)
        if status != "delivered":
            if disposition == "obsolete":
                stored_status = "superseded"
            elif disposition == "deferred" and failures < MAX_FAILURE_ATTEMPTS:
                stored_status = "deferred"
                delay_minutes = 5
        session.query(ScrapeHealthDelivery).filter_by(id=claim["id"], claim_token=claim["token"]).update({
            "status": stored_status, "error_type": error_type,
            "failure_count": failures, "next_attempt_at": now + timedelta(minutes=delay_minutes),
            "delivered_at": now if status == "delivered" else None,
            "claim_token": None, "lease_expires_at": None,
        }, synchronize_session=False)
        session.commit()
    return stored_status


def evaluate_scrape_health():
    """Cheap periodic maintenance and at most ten due webhook dispatches.

    No-observation/stale states are informational; they do not enqueue failure
    alerts. Retry suppression is distinct from accepted delivery. Unconfigured
    routes retry hourly for up to seven days; eight send failures exhaust an
    event. A separate own-service watchdog must detect evaluator stoppage.
    """
    summary = {"status": "ok", "processed": 0, "delivered": 0}
    try:
        now = utc_now()
        with create_isolated_session() as session:
            session.query(ScrapeHealthDelivery).filter(
                ~ScrapeHealthDelivery.status.in_(TERMINAL_DELIVERY_STATUSES),
                ScrapeHealthDelivery.created_at < now - DELIVERY_LIFETIME,
                or_(ScrapeHealthDelivery.lease_expires_at.is_(None), ScrapeHealthDelivery.lease_expires_at <= now),
            ).update({"status": "expired", "claim_token": None, "lease_expires_at": None}, synchronize_session=False)
            due = [row[0] for row in session.query(ScrapeHealthDelivery.id).filter(
                ~ScrapeHealthDelivery.status.in_(TERMINAL_DELIVERY_STATUSES),
                ScrapeHealthDelivery.next_attempt_at <= now,
                or_(ScrapeHealthDelivery.lease_expires_at.is_(None), ScrapeHealthDelivery.lease_expires_at <= now),
            ).order_by(ScrapeHealthDelivery.id).limit(10).all()]
            old_observations = [row[0] for row in session.query(ScrapeHealthObservation.id).filter(
                ScrapeHealthObservation.observed_at < now - timedelta(days=30),
            ).limit(1000).all()]
            if old_observations:
                session.query(ScrapeHealthObservation).filter(ScrapeHealthObservation.id.in_(old_observations)).delete(synchronize_session=False)
            session.commit()
        for delivery_id in due:
            claim = _claim_delivery(delivery_id, utc_now())
            if claim:
                status = _deliver(claim)
                summary["processed"] += 1
                summary["delivered"] += status == "delivered"
        logger.info("Scrape health review completed processed=%d delivered=%d", summary["processed"], summary["delivered"])
    except Exception as exc:
        summary["status"] = "error"
        summary["error_type"] = _safe_error_type(exc)
        logger.warning("Scrape health review failed error_type=%s", summary["error_type"])
    return summary
