import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading
import tempfile
import uuid

import pytest

from services.operation_deadline import OperationDeadlineExceeded, current_deadline, deadline_scope, remaining_seconds
from services.patrol_supervisor import PatrolCancelled, PatrolSupervisor, PatrolSupervisorQuarantined

linux_process_test = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="Actual Linux process ownership test requires readable /proc; synthetic tests remain enabled",
)


def _healthy_fetch(site, url):
    from services.patrol.base_patrol import PatrolResult
    return PatrolResult(price=1234, status="active", reason=str(os.getpid()))


def _json_never_returns(site, url):
    async def fetch():
        await asyncio.Event().wait()
    asyncio.run(fetch())


def _close_never_returns(site, url):
    from services.patrol.base_patrol import PatrolResult
    async def fetch():
        try:
            return PatrolResult(price=100, status="active")
        finally:
            # Even a swallowed cancellation never permits a delayed result.
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue
    return asyncio.run(fetch())


def _term_ignored(site, url):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)


def _spawn_browser_descendant(site, url):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(999)"], start_new_session=True)
    Path(url).write_text(str(child.pid))
    while True:
        time.sleep(0.1)


def _switching_fetch(site, url):
    if url == "hang":
        return _json_never_returns(site, url)
    return _healthy_fetch(site, url)


def _supervisor(fetch=_healthy_fetch, **kwargs):
    return PatrolSupervisor(fetch_callable=fetch, cleanup_seconds=0.2, kill_seconds=0.8, **kwargs)


def test_shared_deadline_is_monotonic_and_nested_scope_cannot_extend():
    assert current_deadline() is None
    deadline = time.monotonic() + 10
    with deadline_scope(deadline):
        with deadline_scope(deadline + 50):
            assert current_deadline() == deadline
            assert 0 < remaining_seconds(20) <= 10
            assert remaining_seconds(0.1) == 0.1
    assert current_deadline() is None


@linux_process_test
def test_spawn_child_reuses_generation_then_recycles_and_reaps():
    supervisor = _supervisor(max_tasks=2)
    try:
        first = supervisor.fetch("mercari", "one", deadline=time.monotonic() + 5)
        second = supervisor.fetch("mercari", "two", deadline=time.monotonic() + 5)
        assert first.price == second.price == 1234
        assert first.reason == second.reason
        old_pid = int(first.reason)
        third = supervisor.fetch("mercari", "three", deadline=time.monotonic() + 5)
        assert third.reason != first.reason
        assert not Path(f"/proc/{old_pid}").exists()
    finally:
        supervisor.close()
    assert supervisor._process is None


@pytest.mark.parametrize("fetch", [_json_never_returns, _close_never_returns, _term_ignored])
@linux_process_test
def test_hung_json_close_or_term_is_killed_before_timeout_returns(fetch):
    supervisor = _supervisor(fetch)
    start = time.monotonic()
    with pytest.raises(OperationDeadlineExceeded):
        supervisor.fetch("mercari", "hang", deadline=start + 3)
    assert time.monotonic() - start < 3.2
    assert supervisor._process is None
    assert not supervisor.quarantined


@linux_process_test
def test_timeout_reaps_detached_browser_descendant(tmp_path):
    pid_file = tmp_path / "browser.pid"
    supervisor = _supervisor(_spawn_browser_descendant)
    with pytest.raises(OperationDeadlineExceeded):
        supervisor.fetch("mercari", str(pid_file), deadline=time.monotonic() + 3)
    assert pid_file.exists()
    assert not Path(f"/proc/{pid_file.read_text()}").exists()
    assert supervisor._process is None


@linux_process_test
def test_new_request_runs_only_after_hung_generation_stopped():
    supervisor = _supervisor(_switching_fetch)
    try:
        with pytest.raises(OperationDeadlineExceeded):
            supervisor.fetch("mercari", "hang", deadline=time.monotonic() + 3)
        assert supervisor._process is None
        result = supervisor.fetch("mercari", "healthy", deadline=time.monotonic() + 5)
        assert result.price == 1234
    finally:
        supervisor.close()


@linux_process_test
def test_scheduler_cancellation_stops_real_child_before_returning():
    supervisor = _supervisor(_json_never_returns)
    start = time.monotonic()
    with pytest.raises(PatrolCancelled):
        supervisor.fetch("mercari", "hang", deadline=start + 5,
                         cancelled=lambda: time.monotonic() - start > 0.5)
    assert supervisor._process is None
    assert time.monotonic() - start < 2


@linux_process_test
def test_unreadable_process_state_quarantines_and_blocks_next_request(monkeypatch):
    import services.patrol_supervisor as module
    supervisor = _supervisor()
    original = module._process_table
    supervisor.fetch("mercari", "healthy", deadline=time.monotonic() + 5)
    monkeypatch.setattr(module, "_process_table", lambda: (_ for _ in ()).throw(PermissionError("unreadable")))
    with pytest.raises(PatrolSupervisorQuarantined):
        supervisor.close()
    assert supervisor.quarantined
    with pytest.raises(PatrolSupervisorQuarantined):
        supervisor.fetch("mercari", "new", deadline=time.monotonic() + 5)
    monkeypatch.setattr(module, "_process_table", original)
    supervisor.close()
    assert supervisor._process is None


def test_synthetic_ownership_retains_reparented_descendant_and_rejects_reused_pid():
    from services.patrol_supervisor import _ProcessIdentity, _owned_processes
    root = _ProcessIdentity(10, 100, 1, 10, 10, "S")
    child = _ProcessIdentity(11, 101, 10, 11, 11, "S")
    known = _owned_processes({10: root, 11: child}, 10, {}, 10)
    assert set(known) == {10, 11}
    reparented = _ProcessIdentity(11, 101, 1, 11, 11, "S")
    reused = _ProcessIdentity(10, 999, 1, 20, 20, "S")
    assert set(_owned_processes({10: reused, 11: reparented}, 10, known, 10)) == {11}
    reused_session = _ProcessIdentity(10, 999, 1, 10, 10, "S")
    unrelated = _ProcessIdentity(12, 999, 10, 10, 10, "S")
    assert set(_owned_processes({10: reused_session, 12: unrelated, 11: reparented}, 10, known, 10)) == {11}


def test_synthetic_termination_signals_descendants_then_reaps_owned_leader(monkeypatch):
    import services.patrol_supervisor as module
    root = module._ProcessIdentity(10, 100, 1, 10, 10, "S")
    child = module._ProcessIdentity(11, 101, 10, 11, 11, "S")
    table = {10: root, 11: child}
    signalled = []
    reaped = []

    class Process:
        pid = 10
        def poll(self):
            reaped.append(True)
            return 0 if 10 not in table else None

    monkeypatch.setattr(module, "_process_table", lambda: dict(table))
    def signal_owned(identity, sig):
        signalled.append((identity.pid, sig))
        table.pop(identity.pid)
    monkeypatch.setattr(module, "_signal_identity", signal_owned)
    module.terminate_owned_process(Process(), deadline=time.monotonic() + 1,
                                   cleanup_seconds=0.1, kill_seconds=0.1, session=10)
    assert signalled == [(11, signal.SIGTERM), (10, signal.SIGTERM)]
    assert reaped
    assert table == {}


def test_synthetic_visibility_loss_attempts_owned_group_stop_but_remains_quarantined(monkeypatch):
    import services.patrol_supervisor as module
    signalled = []
    killed = []

    class Process:
        pid = 10
        def poll(self):
            return None
        def kill(self):
            killed.append(True)
        def wait(self, timeout):
            return 0

    monkeypatch.setattr(module, "_process_table", lambda: (_ for _ in ()).throw(PermissionError("unreadable")))
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: signalled.append((pid, sig)))
    with pytest.raises(PatrolSupervisorQuarantined):
        module.terminate_owned_process(Process(), deadline=time.monotonic() + 1, session=10)
    assert signalled == [(10, signal.SIGKILL)]
    assert killed
    supervisor = object.__new__(PatrolSupervisor)
    supervisor._quarantined = True
    supervisor._lock = threading.Lock()
    with pytest.raises(PatrolSupervisorQuarantined):
        supervisor.fetch("mercari", "never-admit", deadline=time.monotonic() + 1)


def test_pidfd_signal_rechecks_identity_after_descriptor_open(monkeypatch):
    import services.patrol_supervisor as module
    identity = module._ProcessIdentity(10, 100, 1, 10, 10, "S")
    replacement = module._ProcessIdentity(10, 999, 1, 10, 10, "S")
    signals = []
    closed = []
    monkeypatch.setattr(module.os, "pidfd_open", lambda pid, flags: 42)
    monkeypatch.setattr(module, "_process_table", lambda: {10: replacement})
    monkeypatch.setattr(module.signal, "pidfd_send_signal", lambda fd, sig: signals.append((fd, sig)))
    monkeypatch.setattr(module.os, "close", lambda fd: closed.append(fd))
    module._signal_identity(identity, signal.SIGKILL)
    assert signals == []
    assert closed == [42]
    monkeypatch.setattr(module, "_process_table", lambda: {10: identity})
    module._signal_identity(identity, signal.SIGKILL)
    assert signals == [(42, signal.SIGKILL)]


def test_partial_result_file_is_rejected_without_waiting_for_frame_body(tmp_path):
    import services.patrol_supervisor as module
    partial = tmp_path / "result.json"
    partial.write_bytes(b'{"kind":')
    supervisor = object.__new__(PatrolSupervisor)
    supervisor._profile_directory = str(tmp_path)
    supervisor._observe = lambda: {}
    start = time.monotonic()
    with pytest.raises(ValueError):
        supervisor._wait_message(start + 10, None)
    assert time.monotonic() - start < 0.5
    # There is no recv_bytes on the parent response path to get stuck after a
    # partial length header. Atomic rename normally prevents partial JSON too.


def test_latest_progress_preserves_missed_fast_events_and_active_deadline(tmp_path):
    import services.patrol_supervisor as module
    runner = object.__new__(module.SupervisedPatrolRunner)
    runner._profile_directory = str(tmp_path)
    runner._generation = "generation"
    runner._observe = lambda: {}
    runner._last_progress, runner._selected_ids = None, None
    runner._finished_ids, runner._sites = set(), set()
    class Process:
        def poll(self):
            return None
    runner._process = Process()
    now = time.monotonic()
    module._write_atomic_message(tmp_path / "progress.json", {
        "event": "phase", "phase": "db_connect", "generation": "generation", "request_id": "request",
        "all_selected_ids": [1, 2, 3], "finished_ids": [1, 2], "touched_sites": ["mercari"],
        "active_item": 3, "active_deadline": now + 0.1, "active_site": "mercari",
        "summary": {"selected_count": 3, "successful_count": 2},
    })
    with pytest.raises(OperationDeadlineExceeded):
        runner._wait_batch("request", now + 10, None, None)
    assert time.monotonic() - now < 0.5
    assert runner._selected_ids == [1, 2, 3]
    assert runner._finished_ids == {1, 2}
    assert runner._timed_out_item == 3


def test_continuation_counter_and_site_result_conservation():
    from services.patrol_supervisor import _merge_batch_summary
    first = {"selected_count": 3, "successful_count": 1, "error_count": 1, "unprocessed_count": 1,
             "status": "deadline_exceeded", "site_counts": {"mercari": 2, "recordcity": 1},
             "site_results": {"mercari": {"successful_count": 1, "error_count": 1,
                                          "inconclusive_count": 0, "reason": "operation_timeout"}}}
    second = {"selected_count": 1, "successful_count": 1, "error_count": 0, "unprocessed_count": 0,
              "status": "completed", "site_results": {"recordcity": {"successful_count": 1,
                           "error_count": 0, "inconclusive_count": 0, "reason": None}}}
    merged = _merge_batch_summary(first, second)
    assert merged["status"] == "completed"
    assert merged["successful_count"] == 2
    assert merged["error_count"] == 1
    assert merged["unprocessed_count"] == 0
    assert merged["site_results"]["mercari"]["reason"] == "operation_timeout"
    assert merged["site_results"]["recordcity"]["successful_count"] == 1
    empty_continuation = _merge_batch_summary(first, {
        "selected_count": 0, "successful_count": 0, "error_count": 0,
        "unprocessed_count": 0, "status": "no_products", "site_results": {},
    })
    assert empty_continuation["status"] == "completed"
    assert empty_continuation["selected_count"] == 3
    assert empty_continuation["discarded_count"] == 1
    assert empty_continuation["unprocessed_count"] == 0


@pytest.mark.parametrize("ipc_failure", [False, True])
def test_monitor_error_reply_keeps_database_fence_until_owned_stop(monkeypatch, tmp_path, ipc_failure):
    from contextlib import contextmanager
    import services.patrol_supervisor as module
    import services.patrol_ownership as ownership_module
    from services.monitor_service import MonitorService

    class StopOwnedProcess(BaseException):
        pass

    held = {"fence": False}
    replies = []
    @contextmanager
    def ownership(_):
        held["fence"] = True
        class Owner:
            session_factory = None
            def __call__(self):
                return None
        try:
            yield Owner()
        finally:
            held["fence"] = False
    def fail_monitor(**kwargs):
        raise RuntimeError("reporting could not complete")
    def write_message(path, message):
        if message["kind"] == "error":
            assert held["fence"]
            replies.append(message)
            if ipc_failure:
                raise OSError("bounded result disk unavailable")
    def stop_owned_process(_):
        assert held["fence"]
        raise StopOwnedProcess()

    monkeypatch.setattr(module, "_setup_child", lambda *args: None)
    monkeypatch.setattr(module, "_wait_child_request", lambda _: {
        "kind": "batch", "request_id": "request", "limit": 1,
        "deadline": time.monotonic() + 5, "item_timeout": 2,
    })
    monkeypatch.setattr(module, "_write_atomic_message", write_message)
    monkeypatch.setattr(module.time, "sleep", stop_owned_process)
    monkeypatch.setattr(ownership_module, "patrol_ownership", ownership)
    monkeypatch.setattr(MonitorService, "check_stale_products", fail_monitor)
    with pytest.raises(StopOwnedProcess):
        module._patrol_batch_child_main(os.getpid(), "generation", str(tmp_path))
    assert replies[0]["error"] == "RuntimeError"
    assert not held["fence"]


def test_hysteresis_handoff_rejects_invalid_values_and_bounds_retention():
    from services.patrol_supervisor import _bounded_soft_sold_counts, _MAX_HYSTERESIS_PRODUCTS
    assert _bounded_soft_sold_counts({"1": 1, "2": 2, "3": True, "4": 3,
                                     "-5": 1, "bad": 1, 1.5: 1, str(2 ** 63): 1}) == {1: 1, 2: 2}
    assert _bounded_soft_sold_counts([1, 2]) == {}
    retained = _bounded_soft_sold_counts({str(index): 1 for index in range(1, _MAX_HYSTERESIS_PRODUCTS + 100)})
    assert len(retained) == _MAX_HYSTERESIS_PRODUCTS


@linux_process_test
@pytest.mark.parametrize("phase", ["driver_connect", "commit", "response_json", "context_close"])
def test_whole_runner_stops_uncooperative_phase_then_continues_after_reaping(phase):
    """Real Linux ownership, with a controlled stand-in for a stuck native call.

    No marketplace, database server or Chrome is contacted by this fixture.
    Actual psycopg/Playwright integration remains a separate deployment gate.
    """
    import services.patrol_supervisor as module
    script = r'''
import os, signal, sys, time
from pathlib import Path
from services.patrol_supervisor import _setup_child, _wait_child_request, _write_atomic_message
directory, generation, parent, mode, phase = sys.argv[1:]
_setup_child(int(parent), directory)
result = Path(directory) / 'result.json'
_write_atomic_message(result, {'kind':'ready','generation':generation,'pid':os.getpid()})
request = _wait_child_request(directory)
ids = request.get('product_ids') or [1,2]
summary = {'status':'started','limit':len(ids),'eligible_count':len(ids),'selected_count':len(ids),
           'successful_count':0,'error_count':0,'updated_count':0,'attempted_count':1,
           'discarded_count':0,'inconclusive_count':0,'unprocessed_count':len(ids),
           'site_counts':{'mercari':len(ids)},'site_results':{}}
state = {'event':'phase','phase':phase,'generation':generation,'request_id':request['request_id'],
         'all_selected_ids':ids,'finished_ids':[],'active_item':ids[0],'active_site':'mercari',
         'active_deadline':time.monotonic()+request['item_timeout'],'touched_sites':['mercari'],
         'summary':summary}
if mode=='hang':
    _write_atomic_message(Path(directory)/'progress.json',state)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    while True: time.sleep(1)
summary.update(status='completed',successful_count=len(ids),attempted_count=len(ids),unprocessed_count=0)
summary['site_results']={'mercari':{'successful_count':len(ids),'error_count':0,'inconclusive_count':0,'reason':None}}
state.update(finished_ids=ids,active_item=None,active_deadline=None,summary=summary)
_write_atomic_message(Path(directory)/'progress.json',state)
_write_atomic_message(result,{'kind':'batch_result','generation':generation,
                      'request_id':request['request_id'],'summary':summary,'retire':True})
while True: time.sleep(1)
'''
    class FixtureRunner(module.SupervisedPatrolRunner):
        mode = "hang"
        stopped_pids = []
        def _start(self, deadline, cancelled):
            generation = uuid.uuid4().hex
            directory = tempfile.mkdtemp(prefix="esp-patrol-fixture-")
            process = subprocess.Popen([sys.executable, "-c", script, directory, generation,
                                        str(os.getpid()), self.mode, phase], start_new_session=True)
            self.mode = "healthy"
            self._process, self._connection, self._generation = process, None, generation
            self._profile_directory, self._retiring_generation = directory, None
            self._known, self._session, self._sites = {}, process.pid, set()
            self._started_at, self._tasks = time.monotonic(), 0
            assert self._wait_message(deadline, cancelled)["kind"] == "ready"
        def _retire(self, deadline):
            if self._process is not None:
                pid = self._process.pid
                super()._retire(deadline)
                self.stopped_pids.append(pid)
    runner = FixtureRunner(cleanup_seconds=0.2, kill_seconds=0.8)
    start = time.monotonic()
    summary = runner.run_batch(limit=2, deadline=start + 60, item_timeout=3)
    assert time.monotonic() - start < 7
    assert summary["successful_count"] == 1
    assert summary["error_count"] == 1
    assert summary["selected_count"] == 2
    assert summary["unprocessed_count"] == 0
    assert summary["status"] == "completed"
    assert runner._process is None
    assert len(runner.stopped_pids) == 2
    assert all(not Path(f"/proc/{pid}").exists() for pid in runner.stopped_pids)
