"""
What happens to the worker process when its job loop ends.

Seen in production: scraping stopped for everyone and nothing restarted. The RQ
loop had returned, the heartbeat had stopped — health reported the worker
unavailable — and queued jobs sat there. But the scheduler was still running, so
the process never exited, so the platform saw a healthy service and left it
alone. Its log filled with the same error every five minutes:

    RuntimeError: cannot schedule new futures after shutdown

The scheduler has to stop with the loop, or a dead worker looks alive.
"""
import pytest

from services.worker_runtime import stop_worker_scheduler


class _FakeScheduler:
    def __init__(self, *, explode=False):
        self.shutdown_calls = []
        self._explode = explode

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)
        if self._explode:
            raise RuntimeError("scheduler refused to stop")


class _FakeLock:
    def __init__(self, *, explode=False):
        self.closed = False
        self._explode = explode

    def close(self):
        self.closed = True
        if self._explode:
            raise OSError("lock file already gone")


class _FakeApp:
    def __init__(self, **extensions):
        self.extensions = dict(extensions)


class TestStoppingTheScheduler:
    def test_a_running_scheduler_is_stopped_without_waiting(self):
        scheduler = _FakeScheduler()
        app = _FakeApp(esp_scheduler=scheduler, esp_scheduler_started=True)

        assert stop_worker_scheduler(app) is True
        # Waiting on a hung job is the very thing that kept the process alive.
        assert scheduler.shutdown_calls == [False]
        assert app.extensions["esp_scheduler_started"] is False

    def test_the_lock_is_released_so_the_next_process_can_take_it(self):
        lock = _FakeLock()
        app = _FakeApp(
            esp_scheduler=_FakeScheduler(),
            esp_scheduler_started=True,
            esp_scheduler_lock_handle=lock,
        )

        stop_worker_scheduler(app)

        assert lock.closed is True
        assert "esp_scheduler_lock_handle" not in app.extensions

    @pytest.mark.parametrize(
        "extensions",
        [
            {},
            {"esp_scheduler": None, "esp_scheduler_started": True},
            {"esp_scheduler": _FakeScheduler(), "esp_scheduler_started": False},
        ],
    )
    def test_nothing_to_stop_is_not_an_error(self, extensions):
        # The worker runs with the scheduler off in some deployments.
        assert stop_worker_scheduler(_FakeApp(**extensions)) is False

    def test_a_scheduler_that_will_not_stop_does_not_break_shutdown(self):
        # This runs in a finally block; raising here would mask the real reason
        # the worker was shutting down.
        lock = _FakeLock()
        app = _FakeApp(
            esp_scheduler=_FakeScheduler(explode=True),
            esp_scheduler_started=True,
            esp_scheduler_lock_handle=lock,
        )

        assert stop_worker_scheduler(app) is False
        assert app.extensions["esp_scheduler_started"] is False
        assert lock.closed is True

    def test_a_lock_that_will_not_release_does_not_break_shutdown(self):
        app = _FakeApp(
            esp_scheduler=_FakeScheduler(),
            esp_scheduler_started=True,
            esp_scheduler_lock_handle=_FakeLock(explode=True),
        )

        assert stop_worker_scheduler(app) is True


class _StubRuntime:
    """Enough of the worker runtime for run_worker to reach its shutdown."""

    class _Settings:
        burst = False
        with_scheduler = False
        warm_browser_pool = False
        reconcile_stalled_jobs_on_startup = False
        process_selector_repairs_on_startup = False

    class _Worker:
        def __init__(self, on_work):
            self._on_work = on_work

        def work(self, burst=False, with_scheduler=False):
            return self._on_work()

    def __init__(self, on_work):
        self.settings = self._Settings()
        self.worker = self._Worker(on_work)


def _stub_worker_startup(monkeypatch, on_work):
    import services.worker_runtime as worker_runtime

    monkeypatch.setattr(worker_runtime, "ensure_additive_schema_ready", lambda: None)
    monkeypatch.setattr(worker_runtime, "build_worker_runtime", lambda app: _StubRuntime(on_work))
    monkeypatch.setattr(worker_runtime, "start_worker_heartbeat", lambda runtime: None)
    monkeypatch.setattr(worker_runtime, "stop_worker_heartbeat", lambda handle: None)
    monkeypatch.setattr(worker_runtime, "get_job_backlog_snapshot", lambda: {})
    monkeypatch.setattr(worker_runtime, "evaluate_backlog_issues", lambda snapshot, settings: [])
    monkeypatch.setattr(worker_runtime, "get_browser_pool_health", lambda: {"runtimes": []})
    monkeypatch.setattr(worker_runtime, "close_browser_pool", lambda: None)
    return worker_runtime


class TestTheWorkerStopsItOnTheWayOut:
    def test_the_job_loop_returning_stops_the_scheduler(self, app, monkeypatch):
        worker_runtime = _stub_worker_startup(monkeypatch, on_work=lambda: None)
        scheduler = _FakeScheduler()
        app.extensions["esp_scheduler"] = scheduler
        app.extensions["esp_scheduler_started"] = True

        assert worker_runtime.run_worker(app) == 0

        # Otherwise the process stays up doing nothing and is never replaced.
        assert scheduler.shutdown_calls == [False]

    def test_the_loop_failing_stops_it_too(self, app, monkeypatch):
        def _explode():
            raise RuntimeError("redis went away")

        worker_runtime = _stub_worker_startup(monkeypatch, on_work=_explode)
        scheduler = _FakeScheduler()
        app.extensions["esp_scheduler"] = scheduler
        app.extensions["esp_scheduler_started"] = True

        with pytest.raises(RuntimeError, match="redis went away"):
            worker_runtime.run_worker(app)

        assert scheduler.shutdown_calls == [False]
