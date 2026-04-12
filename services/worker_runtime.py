"""
Dedicated worker runtime bootstrap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from flask import Flask
from redis import Redis

from database import ensure_additive_schema_ready
from services.alerts import get_alert_dispatcher
from services.rq_compat import import_rq_queue, import_rq_simple_worker
from services.browser_pool import close_browser_pool, get_browser_pool_health, warm_browser_pool
from services.repair_store import get_repair_queue_snapshot
from services.repair_worker import process_pending_repair_candidates
from services.scrape_job_store import get_job_backlog_snapshot, reconcile_stalled_jobs


logger = logging.getLogger("worker_runtime")


@dataclass(frozen=True)
class WorkerRuntimeSettings:
    queue_backend: str
    queue_name: str
    redis_url: str
    burst: bool
    with_scheduler: bool
    warm_browser_pool: bool
    reconcile_stalled_jobs_on_startup: bool
    process_selector_repairs_on_startup: bool
    selector_repair_limit: int
    backlog_warn_count: int
    backlog_warn_age_seconds: int


@dataclass
class WorkerRuntime:
    settings: WorkerRuntimeSettings
    connection: Redis
    queue: Any
    worker: Any


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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

    return WorkerRuntimeSettings(
        queue_backend=queue_backend,
        queue_name=queue_name,
        redis_url=redis_url,
        burst=burst,
        with_scheduler=with_scheduler,
        warm_browser_pool=warm_browser_pool_enabled,
        reconcile_stalled_jobs_on_startup=reconcile_stalled_jobs_on_startup,
        process_selector_repairs_on_startup=process_selector_repairs_on_startup,
        selector_repair_limit=selector_repair_limit,
        backlog_warn_count=backlog_warn_count,
        backlog_warn_age_seconds=backlog_warn_age_seconds,
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
    queue = Queue(settings.queue_name, connection=connection)
    worker = SimpleWorker([queue], connection=connection)
    return WorkerRuntime(
        settings=settings,
        connection=connection,
        queue=queue,
        worker=worker,
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


def run_worker(app: Flask) -> int:
    with app.app_context():
        ensure_additive_schema_ready()

    runtime = build_worker_runtime(app)
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
        pool_health = get_browser_pool_health()
        if pool_health["runtimes"]:
            logger.info("Worker browser pool closing: health=%s", pool_health)
        close_browser_pool()
    return 0
