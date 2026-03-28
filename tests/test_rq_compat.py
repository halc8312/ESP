import multiprocessing
import os

from services.rq_compat import import_rq_queue, patch_rq_for_windows


def test_patch_rq_for_windows_maps_fork_to_spawn_on_windows():
    patch_rq_for_windows()
    context = multiprocessing.get_context("fork")

    if os.name == "nt":
        assert context.get_start_method() == "spawn"


def test_import_rq_queue_returns_queue_class():
    Queue = import_rq_queue()
    assert Queue.__name__ == "Queue"
