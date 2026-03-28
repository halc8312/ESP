"""
Compatibility helpers for importing RQ on Windows local development.
"""
from __future__ import annotations

import multiprocessing
import os


def patch_rq_for_windows() -> None:
    if os.name != "nt":
        return
    if getattr(multiprocessing, "_esp_rq_windows_patch_applied", False):
        return

    original_get_context = multiprocessing.get_context

    def compat_get_context(method=None):
        if method == "fork":
            method = "spawn"
        return original_get_context(method)

    multiprocessing.get_context = compat_get_context
    multiprocessing._esp_rq_windows_patch_applied = True


def import_rq_queue():
    patch_rq_for_windows()
    from rq import Queue

    return Queue


def import_rq_simple_worker():
    patch_rq_for_windows()
    from rq import SimpleWorker

    return SimpleWorker


def import_rq_job():
    patch_rq_for_windows()
    from rq.job import Job

    return Job


def import_rq_no_such_job_error():
    patch_rq_for_windows()
    from rq.exceptions import NoSuchJobError

    return NoSuchJobError
