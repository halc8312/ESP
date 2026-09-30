"""
Helpers for tracked scrape job execution with periodic heartbeats.
"""
from __future__ import annotations

import os
import logging
from contextvars import ContextVar
import threading
import time
from typing import Any, Callable

from services.scrape_job_store import (
    get_job_record,
    mark_job_completed,
    mark_job_failed,
    mark_job_heartbeat,
    mark_job_running,
    mark_job_progress,
    mark_job_wait,
)


logger = logging.getLogger("scrape_job_runtime")
_current_job_id: ContextVar[str | None] = ContextVar("scrape_job_id", default=None)
_pending_observations: ContextVar[list | None] = ContextVar("scrape_job_observations", default=None)
_last_wait_report: ContextVar[tuple | None] = ContextVar("scrape_job_wait_report", default=None)


def report_current_job_wait(reason: str, retry_after_seconds: float) -> None:
    """Expose a shared-site wait without replacing durable partial products."""
    job_id = _current_job_id.get()
    if job_id is None:
        return
    now = time.monotonic()
    previous = _last_wait_report.get()
    if previous and previous[0] == reason and now - previous[1] < 3:
        return
    if not mark_job_wait(job_id, reason, retry_after_seconds):
        raise ScrapeJobAlreadyTerminated("ジョブは既に終了しています。取得済みの商品を確認してください。")
    _last_wait_report.set((reason, now))


class ScrapeJobAlreadyTerminated(RuntimeError):
    """The durable state was finalized by a watchdog or queue reconciliation."""


def assert_current_job_active() -> None:
    from services.marketplace_access import check_request_budget
    check_request_budget()
    job_id = _current_job_id.get()
    if job_id is not None:
        record = get_job_record(job_id)
        if record is None or record["status"] != "running":
            raise ScrapeJobAlreadyTerminated("ジョブは既に終了しています。取得済みの商品を確認してください。")


def defer_current_job_observation(observation: dict) -> bool:
    pending = _pending_observations.get()
    if pending is None:
        return False
    pending.append(observation)
    return True


def _publish_job_observations() -> None:
    from services.scrape_observation import record_observation_safely
    for observation in _pending_observations.get() or []:
        record_observation_safely(**observation)


def checkpoint_current_job(result: dict, progress: dict) -> None:
    job_id = _current_job_id.get()
    if job_id is not None and not mark_job_progress(job_id, result, progress):
        raise ScrapeJobAlreadyTerminated("ジョブは既に終了しています。取得済みの商品を確認してください。")


def _get_heartbeat_interval_seconds() -> float:
    raw_value = os.environ.get("SCRAPE_JOB_HEARTBEAT_SECONDS", "30")
    try:
        interval = float(raw_value)
    except (TypeError, ValueError):
        interval = 30.0
    return max(5.0, interval)


def _start_heartbeat(job_id: str) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    interval_seconds = _get_heartbeat_interval_seconds()

    def heartbeat_loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                mark_job_heartbeat(job_id)
            except Exception as exc:
                # A transient DB outage must not kill the only heartbeat thread.
                # Do not claim freshness: only a successful DB commit does that.
                logger.warning("Scrape job heartbeat write failed: job=%s error=%s", job_id, type(exc).__name__)

    thread = threading.Thread(
        target=heartbeat_loop,
        name=f"scrape-job-heartbeat-{job_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def run_tracked_job(job_id: str, task_fn: Callable[..., Any], *task_args, **task_kwargs):
    from services.marketplace_access import request_budget
    if mark_job_running(job_id) is False:
        raise RuntimeError("ジョブは既に開始または終了しています。")
    token = _current_job_id.set(job_id)
    observation_token = _pending_observations.set([])
    wait_token = _last_wait_report.set(None)
    stop_event, heartbeat_thread = _start_heartbeat(job_id)
    try:
        try:
            with request_budget(max_requests=120, max_seconds=900):
                result = task_fn(*task_args, **task_kwargs)
        except Exception as exc:
            if mark_job_failed(job_id, str(exc)):
                _publish_job_observations()
            raise
        if mark_job_completed(job_id, result) is False:
            raise ScrapeJobAlreadyTerminated("ジョブは既に終了しています。取得済みの商品を確認してください。")
        _publish_job_observations()
        return result
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=1.0)
        _pending_observations.reset(observation_token)
        _current_job_id.reset(token)
        _last_wait_report.reset(wait_token)
