"""
Helpers for tracked scrape job execution with periodic heartbeats.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable

from services.scrape_job_store import (
    mark_job_completed,
    mark_job_failed,
    mark_job_heartbeat,
    mark_job_running,
)


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
            mark_job_heartbeat(job_id)

    thread = threading.Thread(
        target=heartbeat_loop,
        name=f"scrape-job-heartbeat-{job_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def run_tracked_job(job_id: str, task_fn: Callable[..., Any], *task_args, **task_kwargs):
    mark_job_running(job_id)
    stop_event, heartbeat_thread = _start_heartbeat(job_id)
    try:
        result = task_fn(*task_args, **task_kwargs)
    except Exception as exc:
        mark_job_failed(job_id, str(exc))
        raise
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=1.0)

    mark_job_completed(job_id, result)
    return result
