"""
Dedicated worker runtime bootstrap.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from flask import Flask
from redis import Redis

from app import get_scheduler_health_snapshot
from database import ensure_additive_schema_ready
from services.alerts import get_alert_dispatcher
from services.rq_compat import import_rq_queue, import_rq_simple_worker
from services.browser_pool import close_browser_pool, get_browser_pool_health, warm_browser_pool
from services.repair_store import get_repair_queue_snapshot
from services.repair_worker import process_pending_repair_candidates
from services.scrape_job_store import get_job_backlog_snapshot, reconcile_stalled_jobs
from utils.env_helpers import parse_bool as _as_bool


logger = logging.getLogger("worker_runtime")


@dataclass(frozen=True)
class WorkerRuntimeSettings:
    queue_backend: str
    queue_name: str
    queue_names: tuple[str, ...]
    redis_url: str
    burst: bool
    with_scheduler: bool
    warm_browser_pool: bool
    reconcile_stalled_jobs_on_startup: bool
    process_selector_repairs_on_startup: bool
    selector_repair_limit: int
    backlog_warn_count: int
    backlog_warn_age_seconds: int
    worker_heartbeat_enabled: bool
    worker_heartbeat_key_prefix: str
    worker_heartbeat_interval_seconds: int
    worker_heartbeat_ttl_seconds: int


@dataclass
class WorkerRuntime:
    settings: WorkerRuntimeSettings
    connection: Redis
    queue: Any
    worker: Any


@dataclass
class WorkerHeartbeatHandle:
    connection: Redis
    key: str
    instance_id: str
    interval_seconds: int
    ttl_seconds: int
    stop_event: threading.Event
    thread: threading.Thread | None = None


HEARTBEAT_OK = "ok"
HEARTBEAT_UNAVAILABLE = "unavailable"
HEARTBEAT_STALE = "stale"
HEARTBEAT_FAILED = "failed"
_HEARTBEAT_FUTURE_SKEW_SECONDS = 60
_SCHEDULER_LIVE_EVENTS = frozenset(
    {
        "scheduler_started",
        "scheduler_already_started",
        "scheduler_retry_succeeded",
        "patrol_started",
        "patrol_completed",
        "patrol_failed",
        "translation_recovery_completed",
        "translation_recovery_failed",
    }
)


def _as_int(value, default: int, minimum: int = 0) -> int:
    if value is None or value == "":
        return max(minimum, default)
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return max(minimum, default)


def _collect_worker_runtime_settings(app: Flask, *, require_rq_backend: bool) -> WorkerRuntimeSettings:
    queue_backend = str(app.config.get("SCRAPE_QUEUE_BACKEND", "inmemory") or "inmemory").strip().lower()
    if require_rq_backend and queue_backend != "rq":
        raise RuntimeError("Dedicated worker runtime requires SCRAPE_QUEUE_BACKEND=rq")

    queue_name = str(app.config.get("SCRAPE_QUEUE_NAME", "scrape") or "scrape").strip()
    media_queue_name = str(app.config.get("MEDIA_QUEUE_NAME", "") or "").strip() or queue_name
    queue_names: list[str] = []
    for candidate in (queue_name, media_queue_name):
        if candidate and candidate not in queue_names:
            queue_names.append(candidate)
    redis_url = str(app.config.get("REDIS_URL", "redis://localhost:6379/0") or "redis://localhost:6379/0").strip()
    burst = _as_bool(app.config.get("RQ_BURST", False))
    with_scheduler = _as_bool(app.config.get("RQ_WITH_SCHEDULER", False))
    warm_browser_pool_enabled = _as_bool(app.config.get("WARM_BROWSER_POOL", False))
    reconcile_stalled_jobs_on_startup = _as_bool(
        app.config.get("WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP", True),
        default=True,
    )
    process_selector_repairs_on_startup = _as_bool(
        app.config.get("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", False),
        default=False,
    )
    selector_repair_limit = _as_int(
        app.config.get("WORKER_SELECTOR_REPAIR_LIMIT", 1),
        default=1,
        minimum=1,
    )
    backlog_warn_count = _as_int(app.config.get("WORKER_BACKLOG_WARN_COUNT", 25), default=25, minimum=0)
    backlog_warn_age_seconds = _as_int(
        app.config.get("WORKER_BACKLOG_WARN_AGE_SECONDS", 900),
        default=900,
        minimum=0,
    )
    worker_heartbeat_enabled = _as_bool(
        app.config.get("WORKER_HEARTBEAT_ENABLED", False),
        default=False,
    )
    worker_heartbeat_key_prefix = str(
        app.config.get("WORKER_HEARTBEAT_KEY_PREFIX", "esp:worker:heartbeat")
        or "esp:worker:heartbeat"
    ).strip().rstrip(":") or "esp:worker:heartbeat"
    worker_heartbeat_interval_seconds = _as_int(
        app.config.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", 15),
        default=15,
        minimum=1,
    )
    worker_heartbeat_ttl_seconds = _as_int(
        app.config.get("WORKER_HEARTBEAT_TTL_SECONDS", 90),
        default=90,
        minimum=worker_heartbeat_interval_seconds * 2,
    )

    return WorkerRuntimeSettings(
        queue_backend=queue_backend,
        queue_name=queue_name,
        queue_names=tuple(queue_names),
        redis_url=redis_url,
        burst=burst,
        with_scheduler=with_scheduler,
        warm_browser_pool=warm_browser_pool_enabled,
        reconcile_stalled_jobs_on_startup=reconcile_stalled_jobs_on_startup,
        process_selector_repairs_on_startup=process_selector_repairs_on_startup,
        selector_repair_limit=selector_repair_limit,
        backlog_warn_count=backlog_warn_count,
        backlog_warn_age_seconds=backlog_warn_age_seconds,
        worker_heartbeat_enabled=worker_heartbeat_enabled,
        worker_heartbeat_key_prefix=worker_heartbeat_key_prefix,
        worker_heartbeat_interval_seconds=worker_heartbeat_interval_seconds,
        worker_heartbeat_ttl_seconds=worker_heartbeat_ttl_seconds,
    )


def load_worker_runtime_settings(app: Flask) -> WorkerRuntimeSettings:
    return _collect_worker_runtime_settings(app, require_rq_backend=True)


def build_worker_runtime(app: Flask) -> WorkerRuntime:
    settings = load_worker_runtime_settings(app)
    Queue = import_rq_queue()
    SimpleWorker = import_rq_simple_worker()
    connection = Redis.from_url(settings.redis_url)
    ping = getattr(connection, "ping", None)
    if callable(ping):
        ping()
    queue_names = list(settings.queue_names) or [settings.queue_name]
    queues = [Queue(name, connection=connection) for name in queue_names]
    worker = SimpleWorker(queues, connection=connection)
    return WorkerRuntime(
        settings=settings,
        connection=connection,
        queue=queues[0],
        worker=worker,
    )


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_redis_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_redis_mapping(mapping: dict[Any, Any]) -> dict[str, str]:
    return {
        _decode_redis_value(key): _decode_redis_value(value)
        for key, value in mapping.items()
    }


def _parse_heartbeat_timestamp(raw_value: Any) -> datetime | None:
    normalized = _decode_redis_value(raw_value).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _heartbeat_payload_is_fresh(
    payload: dict[str, Any],
    *,
    freshness_seconds: int,
    now: datetime | None = None,
) -> bool:
    if _decode_redis_value(payload.get("runtime_role", "")).strip().lower() != "worker":
        return False
    recorded_at = _parse_heartbeat_timestamp(payload.get("recorded_at", ""))
    if recorded_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    age_seconds = (current_time - recorded_at).total_seconds()
    return -_HEARTBEAT_FUTURE_SKEW_SECONDS <= age_seconds <= max(1, freshness_seconds)


def inspect_worker_heartbeat(
    connection: Any,
    *,
    key_prefix: str,
    freshness_seconds: int,
    now: datetime | None = None,
) -> str:
    """Return a non-secret readiness state for all live worker heartbeat keys."""
    normalized_prefix = (
        str(key_prefix or "esp:worker:heartbeat").strip().rstrip(":")
        or "esp:worker:heartbeat"
    )
    try:
        saw_key = False
        for key in connection.scan_iter(match=f"{normalized_prefix}:*"):
            saw_key = True
            raw_payload = connection.get(key)
            if raw_payload is None:
                continue
            ttl_seconds = int(connection.ttl(key))
            if ttl_seconds <= 0:
                continue
            try:
                payload = json.loads(_decode_redis_value(raw_payload))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and _heartbeat_payload_is_fresh(
                payload,
                freshness_seconds=max(1, int(freshness_seconds)),
                now=now,
            ):
                return HEARTBEAT_OK
        return HEARTBEAT_STALE if saw_key else HEARTBEAT_UNAVAILABLE
    except Exception as exc:
        logger.warning("Worker heartbeat inspection failed: error=%s", type(exc).__name__)
        return HEARTBEAT_UNAVAILABLE


def inspect_scheduler_heartbeat(
    connection: Any,
    *,
    key: str,
    freshness_seconds: int,
    now: datetime | None = None,
) -> str:
    """Return a non-secret readiness state for the dedicated scheduler."""
    try:
        raw_payload = connection.hgetall(key)
        if not raw_payload:
            return HEARTBEAT_UNAVAILABLE
        payload = _decode_redis_mapping(raw_payload)
        event = payload.get("event", "").strip().lower()
        if event in _SCHEDULER_LIVE_EVENTS and _heartbeat_payload_is_fresh(
            payload,
            freshness_seconds=max(1, int(freshness_seconds)),
            now=now,
        ):
            return HEARTBEAT_OK
        return HEARTBEAT_STALE
    except Exception as exc:
        logger.warning("Scheduler heartbeat inspection failed: error=%s", type(exc).__name__)
        return HEARTBEAT_UNAVAILABLE


def inspect_patrol_heartbeat(
    connection: Any,
    *,
    key: str,
    freshness_seconds: int,
    now: datetime | None = None,
) -> str:
    """Return readiness for completed patrol work, independent of other jobs."""
    try:
        raw_payload = connection.hgetall(key)
        if not raw_payload:
            return HEARTBEAT_UNAVAILABLE
        payload = _decode_redis_mapping(raw_payload)
        if payload.get("runtime_role", "").strip().lower() != "worker":
            return HEARTBEAT_STALE

        completed_at = _parse_heartbeat_timestamp(
            payload.get("last_patrol_completed_at", "")
        )
        failed_at = _parse_heartbeat_timestamp(
            payload.get("last_patrol_failed_at", "")
        )
        if failed_at is not None and (
            completed_at is None or failed_at >= completed_at
        ):
            return HEARTBEAT_FAILED
        if completed_at is None:
            return HEARTBEAT_UNAVAILABLE

        current_time = now or datetime.now(timezone.utc)
        age_seconds = (current_time - completed_at).total_seconds()
        if not (
            -_HEARTBEAT_FUTURE_SKEW_SECONDS
            <= age_seconds
            <= max(1, int(freshness_seconds))
        ):
            return HEARTBEAT_STALE

        status = payload.get("last_patrol_status", "").strip().lower()
        if status not in {"completed", "no_products"}:
            return HEARTBEAT_FAILED

        try:
            selected_count = int(payload.get("last_patrol_selected_count", "0"))
            error_count = int(payload.get("last_patrol_error_count", "0"))
        except (TypeError, ValueError):
            return HEARTBEAT_FAILED
        if selected_count > 0 and error_count >= selected_count:
            return HEARTBEAT_FAILED
        return HEARTBEAT_OK
    except Exception as exc:
        logger.warning("Patrol heartbeat inspection failed: error=%s", type(exc).__name__)
        return HEARTBEAT_UNAVAILABLE


def _build_worker_heartbeat_identity(settings: WorkerRuntimeSettings) -> tuple[str, str]:
    instance_id = uuid.uuid4().hex
    hostname = re.sub(r"[^A-Za-z0-9_.-]+", "-", socket.gethostname()).strip("-") or "unknown"
    key = f"{settings.worker_heartbeat_key_prefix}:{hostname}:{os.getpid()}:{instance_id}"
    return instance_id, key


def _publish_worker_heartbeat(handle: WorkerHeartbeatHandle) -> None:
    payload = {
        "instance_id": handle.instance_id,
        "runtime_role": "worker",
        "recorded_at": _utc_iso_now(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
    }
    handle.connection.set(
        handle.key,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ex=handle.ttl_seconds,
    )


def _run_worker_heartbeat_loop(handle: WorkerHeartbeatHandle) -> None:
    while not handle.stop_event.wait(handle.interval_seconds):
        try:
            _publish_worker_heartbeat(handle)
        except Exception as exc:
            logger.warning(
                "Worker heartbeat write failed: error=%s",
                type(exc).__name__,
            )


def start_worker_heartbeat(runtime: WorkerRuntime) -> WorkerHeartbeatHandle | None:
    settings = runtime.settings
    if not settings.worker_heartbeat_enabled:
        return None

    instance_id, key = _build_worker_heartbeat_identity(settings)
    stop_event = threading.Event()
    handle = WorkerHeartbeatHandle(
        connection=runtime.connection,
        key=key,
        instance_id=instance_id,
        interval_seconds=settings.worker_heartbeat_interval_seconds,
        ttl_seconds=settings.worker_heartbeat_ttl_seconds,
        stop_event=stop_event,
    )
    heartbeat_thread = threading.Thread(
        target=_run_worker_heartbeat_loop,
        args=(handle,),
        name=f"worker-heartbeat-{instance_id[:8]}",
        daemon=True,
    )
    handle.thread = heartbeat_thread
    try:
        _publish_worker_heartbeat(handle)
    except Exception as exc:
        logger.warning(
            "Initial worker heartbeat write failed: error=%s",
            type(exc).__name__,
        )
    heartbeat_thread.start()
    return handle


def stop_worker_heartbeat(handle: WorkerHeartbeatHandle | None) -> None:
    if handle is None:
        return

    handle.stop_event.set()
    if handle.thread is not None:
        handle.thread.join(timeout=5)
    try:
        raw_payload = handle.connection.get(handle.key)
        if raw_payload is None:
            return
        payload = json.loads(_decode_redis_value(raw_payload))
        if isinstance(payload, dict) and payload.get("instance_id") == handle.instance_id:
            handle.connection.delete(handle.key)
    except Exception as exc:
        logger.warning(
            "Worker heartbeat cleanup failed: error=%s",
            type(exc).__name__,
        )


def get_worker_health_snapshot(app: Flask) -> dict[str, Any]:
    settings = _collect_worker_runtime_settings(app, require_rq_backend=False)
    backlog = get_job_backlog_snapshot()
    browser_pool_health = get_browser_pool_health()
    backlog_issues = evaluate_backlog_issues(backlog, settings)
    selector_repairs = get_repair_queue_snapshot() if settings.queue_backend == "rq" else None
    repair_issues: list[str] = []
    if selector_repairs and not list(selector_repairs.get("blockers") or []):
        pending_count = int(selector_repairs.get("pending_count") or 0)
        if pending_count > 0:
            repair_issues.append(f"pending_selector_repairs={pending_count}")

    snapshot: dict[str, Any] = {
        "runtime_role": str(app.config.get("ESP_RUNTIME_ROLE", "")),
        "queue_backend": settings.queue_backend,
        "queue_name": settings.queue_name,
        "redis_url": settings.redis_url,
        "scheduler_enabled": bool(app.config.get("ENABLE_SCHEDULER", False)),
        "scheduler": get_scheduler_health_snapshot(app),
        "rq_with_scheduler": settings.with_scheduler,
        "warm_browser_pool": bool(settings.warm_browser_pool),
        "backlog": backlog,
        "backlog_issues": backlog_issues,
        "backlog_thresholds": {
            "warn_count": settings.backlog_warn_count,
            "warn_age_seconds": settings.backlog_warn_age_seconds,
        },
        "browser_pool_health": browser_pool_health,
        "selector_repairs": selector_repairs,
        "repair_issues": repair_issues,
        "selector_alert_enabled": bool(getattr(get_alert_dispatcher(), "selector_webhook_url", "")),
        "operational_alert_enabled": bool(getattr(get_alert_dispatcher(), "operational_webhook_url", "")),
        "worker_runtime_supported": settings.queue_backend == "rq",
    }

    if settings.queue_backend != "rq":
        snapshot["redis_ok"] = None
        snapshot["redis_error"] = None
        return snapshot

    try:
        connection = Redis.from_url(settings.redis_url)
        ping = getattr(connection, "ping", None)
        if callable(ping):
            ping()
        snapshot["redis_ok"] = True
        snapshot["redis_error"] = None
    except Exception as exc:
        snapshot["redis_ok"] = False
        snapshot["redis_error"] = str(exc)

    return snapshot


def evaluate_backlog_issues(snapshot: dict[str, Any], settings: WorkerRuntimeSettings) -> list[str]:
    issues: list[str] = []

    queued_count = int(snapshot.get("queued_count") or 0)
    oldest_queued_age_seconds = snapshot.get("oldest_queued_age_seconds")
    oldest_running_age_seconds = snapshot.get("oldest_running_age_seconds")

    if settings.backlog_warn_count > 0 and queued_count >= settings.backlog_warn_count:
        issues.append(f"queued_count>={settings.backlog_warn_count}")

    if settings.backlog_warn_age_seconds > 0:
        if oldest_queued_age_seconds is not None and float(oldest_queued_age_seconds) >= settings.backlog_warn_age_seconds:
            issues.append(f"oldest_queued_age_seconds>={settings.backlog_warn_age_seconds}")
        if oldest_running_age_seconds is not None and float(oldest_running_age_seconds) >= settings.backlog_warn_age_seconds:
            issues.append(f"oldest_running_age_seconds>={settings.backlog_warn_age_seconds}")

    return issues


def emit_backlog_operational_alert(
    snapshot: dict[str, Any],
    issues: list[str],
    settings: WorkerRuntimeSettings,
) -> bool:
    if not issues:
        return False

    try:
        return get_alert_dispatcher().notify_operational_issue(
            event_type="worker_backlog_warning",
            component="worker_runtime",
            severity="warning",
            message="Worker durable job backlog exceeded warning thresholds.",
            details={
                "issues": list(issues),
                "queue_name": settings.queue_name,
                "redis_url": settings.redis_url,
                "backlog": snapshot,
            },
            dedupe_key=f"worker_backlog_warning:{settings.queue_name}:{','.join(issues)}",
        )
    except Exception as exc:
        logger.warning("Operational backlog alert dispatch failed: %s", exc)
        return False


def recover_translation_suggestions_on_startup() -> dict[str, Any]:
    """Load translation recovery lazily so worker bootstrap remains decoupled."""
    from services.translator.suggestion_store import (
        recover_expired_translation_suggestions,
    )

    return recover_expired_translation_suggestions()


def run_worker(app: Flask) -> int:
    with app.app_context():
        ensure_additive_schema_ready()

    runtime = build_worker_runtime(app)
    heartbeat_handle = start_worker_heartbeat(runtime)
    try:
        with app.app_context():
            backlog_before = get_job_backlog_snapshot()
            logger.info(
                "Worker durable job backlog before startup reconcile: backlog=%s",
                backlog_before,
            )
            backlog_before_issues = evaluate_backlog_issues(backlog_before, runtime.settings)
            if backlog_before_issues:
                logger.warning(
                    "Worker durable job backlog warning before startup reconcile: issues=%s backlog=%s",
                    backlog_before_issues,
                    backlog_before,
                )
            if runtime.settings.reconcile_stalled_jobs_on_startup:
                reconciled_job_ids = reconcile_stalled_jobs()
                if reconciled_job_ids:
                    logger.warning(
                        "Reconciled stalled scrape jobs before worker start: count=%s job_ids=%s",
                        len(reconciled_job_ids),
                        ",".join(reconciled_job_ids[:10]),
                    )
                try:
                    translation_recovery = recover_translation_suggestions_on_startup()
                    logger.info(
                        "Translation startup recovery complete: recovered=%s enqueued=%s failed=%s",
                        len(translation_recovery.get("recovered") or []),
                        len(translation_recovery.get("enqueued") or []),
                        len(translation_recovery.get("failed") or []),
                    )
                except Exception:
                    logger.exception(
                        "Translation startup recovery failed; worker startup will continue"
                    )
            backlog_after = get_job_backlog_snapshot()
            logger.info(
                "Worker durable job backlog after startup reconcile: backlog=%s",
                backlog_after,
            )
            backlog_after_issues = evaluate_backlog_issues(backlog_after, runtime.settings)
            if backlog_after_issues:
                logger.warning(
                    "Worker durable job backlog warning after startup reconcile: issues=%s backlog=%s",
                    backlog_after_issues,
                    backlog_after,
                )
                emit_backlog_operational_alert(backlog_after, backlog_after_issues, runtime.settings)
        if runtime.settings.warm_browser_pool:
            warmed_result = warm_browser_pool()
            warmed_sites = list(warmed_result) if isinstance(warmed_result, (list, tuple)) else []
            logger.info(
                "Worker browser pool warmed: sites=%s health=%s",
                ",".join(warmed_sites) or "(none)",
                get_browser_pool_health(),
            )
        if runtime.settings.process_selector_repairs_on_startup:
            with app.app_context():
                repair_summary = process_pending_repair_candidates(limit=runtime.settings.selector_repair_limit)
                logger.info("Worker selector repair startup summary: %s", repair_summary)
        runtime.worker.work(
            burst=runtime.settings.burst,
            with_scheduler=runtime.settings.with_scheduler,
        )
    finally:
        stop_worker_heartbeat(heartbeat_handle)
        pool_health = get_browser_pool_health()
        if pool_health["runtimes"]:
            logger.info("Worker browser pool closing: health=%s", pool_health)
        close_browser_pool()
    return 0
