from datetime import datetime, timedelta, timezone
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from services import recovery_health
from services.recovery_health import (
    OwnedRecoveryExecutor,
    RecoveryCleanupIncomplete,
    RecoveryCancelled,
    RecoveryTimeout,
    inspect_recovery_heartbeats,
    run_recovery_phases,
)


# An absolute tolerance keeps long-running monotonic clocks from relaxing budgets.
CLOCK_TOLERANCE_SECONDS = 1e-9


class Heartbeats:
    def __init__(self):
        self.payload = {"runtime_role": "worker"}
        self.events = []

    def write(self, **fields):
        self.events.append(fields)
        self.payload.update({key: str(value) for key, value in fields.items()})

    def hgetall(self, _key):
        return self.payload


def test_partial_failure_is_not_masked_by_translation_success():
    heartbeats = Heartbeats()
    old_success = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    heartbeats.payload["last_recovery_thumbnail_completed_at"] = old_success

    def fail_thumbnail():
        raise ConnectionError("private diagnostics must not appear in heartbeat")

    result = run_recovery_phases({
        "notification": lambda: {"considered": 0, "enqueued": 0},
        "thumbnail": fail_thumbnail,
        "detail": lambda: {"queued": 1, "failed": 0},
        "translation": lambda: {"recovered": ["job"], "enqueued": ["job"], "failed": []},
    }, heartbeat_writer=heartbeats.write)

    assert result["status"] == "degraded"
    states = inspect_recovery_heartbeats(heartbeats, key="test")
    assert states == {
        "recovery_notification": "no_pending", "recovery_thumbnail": "failed",
        "recovery_detail": "ok", "recovery_translation": "ok",
    }
    assert not all(value in recovery_health.RECOVERY_HEALTHY_STATES for value in states.values())
    assert heartbeats.payload["last_recovery_thumbnail_completed_at"] == old_success
    assert heartbeats.payload["last_recovery_thumbnail_error"] == "ConnectionError"
    assert "private diagnostics" not in str(heartbeats.payload)


def test_empty_scan_is_no_pending_and_has_no_delivery_success_count():
    heartbeats = Heartbeats()
    result = run_recovery_phases({
        "translation": lambda: {"recovered": [], "enqueued": [], "failed": []},
    }, heartbeat_writer=heartbeats.write)

    assert result["status"] == "ok"
    assert result["features"]["translation"]["status"] == "no_pending"
    assert heartbeats.payload["last_recovery_translation_no_pending_count"] == "1"
    assert heartbeats.payload["last_recovery_translation_enqueued_count"] == "0"
    assert "last_recovery_translation_successful_count" not in heartbeats.payload


def test_returned_enqueue_failure_degrades_aggregate():
    heartbeats = Heartbeats()
    result = run_recovery_phases({
        "thumbnail": lambda: {"queued_batches": 1, "queued_images": 10, "failed": 1},
        "translation": lambda: {"recovered": [], "enqueued": [], "failed": []},
    }, heartbeat_writer=heartbeats.write)

    assert result["status"] == "degraded"
    assert inspect_recovery_heartbeats(heartbeats, key="test")["recovery_thumbnail"] == "degraded"
    assert "last_recovery_thumbnail_completed_at" not in heartbeats.payload


def test_recovered_feature_clears_only_its_error():
    heartbeats = Heartbeats()
    run_recovery_phases({
        "thumbnail": lambda: {"failed": 1}, "detail": lambda: {"failed": 1},
    }, heartbeat_writer=heartbeats.write)
    run_recovery_phases({
        "thumbnail": lambda: {"queued_batches": 1, "queued_images": 2, "failed": 0},
    }, heartbeat_writer=heartbeats.write)
    states = inspect_recovery_heartbeats(heartbeats, key="test")
    assert states["recovery_thumbnail"] == "ok"
    assert states["recovery_detail"] == "degraded"
    assert heartbeats.payload["last_recovery_thumbnail_error"] == ""


def test_hung_phase_stales_independently_of_live_scheduler():
    heartbeats = Heartbeats()
    now = datetime.now(timezone.utc)
    heartbeats.write(event="recovery_phase_started", recorded_at=now.isoformat(),
        last_recovery_detail_started_at=(now - timedelta(seconds=80)).isoformat(),
        last_recovery_detail_timeout_seconds=60, last_recovery_detail_status="running")
    assert inspect_recovery_heartbeats(heartbeats, key="test", now=now)["recovery_detail"] == "stale"


def test_cleanup_failure_prevents_next_operation():
    heartbeats = Heartbeats()
    calls = []

    def unverified_cleanup(_feature, _operation, _timeout):
        calls.append("started")
        raise RecoveryCleanupIncomplete("owned child remains")

    result = run_recovery_phases({
        "thumbnail": lambda: {}, "translation": lambda: calls.append("unsafe"),
    }, heartbeat_writer=heartbeats.write, executor=unverified_cleanup)
    assert calls == ["started"]
    assert result["features"]["translation"]["status"] == "quarantined"


def test_batch_deadline_prevents_admission_of_later_phase():
    clock = [0.0]
    heartbeats = Heartbeats()
    calls = []

    def executor(feature, operation, allowance):
        calls.append((feature, allowance))
        clock[0] += allowance
        return operation()

    result = run_recovery_phases({"detail": lambda: {}, "translation": lambda: {}},
        heartbeat_writer=heartbeats.write, executor=executor,
        monotonic=lambda: clock[0], phase_timeout_seconds=2, total_timeout_seconds=2)
    assert [feature for feature, _allowance in calls] == ["detail"]
    assert [allowance for _feature, allowance in calls] == pytest.approx(
        [2], rel=0, abs=CLOCK_TOLERANCE_SECONDS)
    assert result["features"]["translation"]["error_type"] == "RecoveryTimeout"


def test_phase_allowance_includes_started_heartbeat_and_reserves_completion(monkeypatch):
    started = 1023.123456789
    clock = [started]
    monkeypatch.setattr(recovery_health.time, "monotonic", lambda: clock[0])
    calls = []
    heartbeats = Heartbeats()

    def writer(**fields):
        heartbeats.write(**fields)
        if fields["event"] in {"recovery_phase_started", "recovery_phase_completed"}:
            clock[0] += 5

    def executor(feature, operation, allowance):
        calls.append((feature, allowance))
        clock[0] += allowance
        return operation()

    result = run_recovery_phases({"detail": lambda: {}}, heartbeat_writer=writer,
        executor=executor, monotonic=lambda: clock[0], phase_timeout_seconds=60,
        total_timeout_seconds=240, phase_heartbeat_reserve_seconds=5)
    assert result["status"] == "ok"
    assert [feature for feature, _allowance in calls] == ["detail"]
    assert [allowance for _feature, allowance in calls] == pytest.approx(
        [50], rel=0, abs=CLOCK_TOLERANCE_SECONDS)
    assert clock[0] - started == pytest.approx(60, rel=0, abs=CLOCK_TOLERANCE_SECONDS)


@pytest.mark.parametrize("started", [0.0, 900.123456789, 1023.123456789])
def test_saturated_four_phases_leave_full_aggregate_heartbeat_budget(started, monkeypatch):
    clock = [started]
    monkeypatch.setattr(recovery_health.time, "monotonic", lambda: clock[0])
    allowances = []
    aggregate_started = []

    def writer(**fields):
        if fields["event"] == "recovery_batch_completed":
            aggregate_started.append(clock[0])
        clock[0] += 5
        assert clock[0] - started <= 240 + CLOCK_TOLERANCE_SECONDS

    def executor(feature, operation, allowance):
        allowances.append((feature, allowance))
        clock[0] += allowance
        return operation()

    result = run_recovery_phases({feature: lambda: {} for feature in recovery_health.RECOVERY_FEATURES},
        heartbeat_writer=writer, executor=executor, monotonic=lambda: clock[0],
        phase_timeout_seconds=60, total_timeout_seconds=240,
        phase_heartbeat_reserve_seconds=5)
    assert result["status"] == "ok"
    assert [feature for feature, _allowance in allowances] == [
        "notification", "thumbnail", "detail", "translation"]
    assert [allowance for _feature, allowance in allowances] == pytest.approx(
        [50, 50, 50, 45], rel=0, abs=CLOCK_TOLERANCE_SECONDS)
    assert [timestamp - started for timestamp in aggregate_started] == pytest.approx(
        [235], rel=0, abs=CLOCK_TOLERANCE_SECONDS)
    assert clock[0] - started == pytest.approx(240, rel=0, abs=CLOCK_TOLERANCE_SECONDS)


def test_late_started_phases_cannot_spend_final_aggregate_reserve(monkeypatch):
    from services.operation_deadline import current_deadline

    started = 1023.123456789
    clock = [started]
    monkeypatch.setattr(recovery_health.time, "monotonic", lambda: clock[0])
    calls, writes = [], []

    def writer(**fields):
        writes.append(fields)
        active_deadline = current_deadline()
        if fields["event"] == "recovery_batch_completed":
            assert active_deadline is None
            assert clock[0] - started == pytest.approx(55, rel=0, abs=CLOCK_TOLERANCE_SECONDS)
        else:
            assert active_deadline - started == pytest.approx(
                55, rel=0, abs=CLOCK_TOLERANCE_SECONDS)
        clock[0] += 5
        assert clock[0] - started <= 60 + CLOCK_TOLERANCE_SECONDS

    def saturate(feature, _operation, allowance):
        calls.append((feature, allowance))
        clock[0] += allowance
        raise RecoveryTimeout("Simulated child deadline")

    result = run_recovery_phases({feature: lambda: {} for feature in recovery_health.RECOVERY_FEATURES},
        heartbeat_writer=writer, executor=saturate, monotonic=lambda: clock[0],
        phase_timeout_seconds=60, total_timeout_seconds=60,
        phase_heartbeat_reserve_seconds=5)
    assert [feature for feature, _allowance in calls] == ["notification"]
    assert [allowance for _feature, allowance in calls] == pytest.approx(
        [45], rel=0, abs=CLOCK_TOLERANCE_SECONDS)
    assert clock[0] - started == pytest.approx(60, rel=0, abs=CLOCK_TOLERANCE_SECONDS)
    assert len(writes) == 3
    aggregate = writes[-1]
    assert aggregate["last_recovery_status"] == "degraded"
    assert all(aggregate[f"last_recovery_{feature}_status"] == "failed"
        for feature in recovery_health.RECOVERY_FEATURES)
    assert all(value["status"] == "failed" for value in result["features"].values())


def test_started_heartbeat_exhausting_budget_prevents_child_admission():
    clock = [time.monotonic()]
    calls = []

    def writer(**fields):
        if fields["event"] == "recovery_phase_started":
            clock[0] += 5

    result = run_recovery_phases({"detail": lambda: calls.append("unsafe")},
        heartbeat_writer=writer, executor=lambda *_args: calls.append("unsafe"),
        monotonic=lambda: clock[0], phase_timeout_seconds=5, total_timeout_seconds=5,
        phase_heartbeat_reserve_seconds=1)
    assert calls == []
    assert result["features"]["detail"]["error_type"] == "RecoveryTimeout"


@pytest.mark.parametrize("setting", [float("nan"), float("inf"), -1])
def test_nonfinite_recovery_budget_is_rejected(setting):
    with pytest.raises(ValueError):
        run_recovery_phases({"detail": lambda: {}}, heartbeat_writer=lambda **_fields: None,
            total_timeout_seconds=setting)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux /proc ownership view unavailable")
def test_owned_executor_stops_and_reaps_hung_process(monkeypatch):
    processes = []
    real_popen = subprocess.Popen

    def hung_child(_args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(recovery_health.subprocess, "Popen", hung_child)
    started = time.monotonic()
    with pytest.raises(RecoveryTimeout):
        OwnedRecoveryExecutor({"SCRAPE_QUEUE_BACKEND": "rq"})("detail", lambda: {}, 0.9)
    assert time.monotonic() - started < 1.4
    assert processes[0].poll() is not None


def test_unverified_session_remains_quarantined_after_leader_exits(monkeypatch):
    class DeadLeader:
        def poll(self):
            return 0

    child = recovery_health._OwnedRecoveryChild(DeadLeader(), {12: "retained identity"})
    monkeypatch.setattr(recovery_health, "_unreaped_children", [child])
    monkeypatch.setattr(recovery_health, "_stop_owned_process", lambda *_args, **_kwargs: False)
    with pytest.raises(RecoveryCleanupIncomplete):
        OwnedRecoveryExecutor({"SCRAPE_QUEUE_BACKEND": "rq"})("detail", lambda: {}, 1)
    assert recovery_health._unreaped_children == [child]


def test_periodic_ownership_observation_retains_reparented_detached_child(monkeypatch):
    """Injected snapshots verify identity retention, not actual Linux visibility."""
    import services.patrol_supervisor as supervisor

    root = supervisor._ProcessIdentity(100, 1, 10, 100, 100, "S")
    child = supervisor._ProcessIdentity(101, 2, 100, 100, 100, "S")
    detached = supervisor._ProcessIdentity(101, 2, 1, 101, 101, "S")
    snapshots = iter([{100: root, 101: child}, {101: detached}])
    monkeypatch.setattr(supervisor, "_process_table", lambda: next(snapshots))
    known = {}
    process = type("Process", (), {"pid": 100})()
    recovery_health._observe_owned_process(process, known)
    recovery_health._observe_owned_process(process, known)
    assert known[101] == detached
    assert known[101].started == 2


def test_reused_child_pid_does_not_inherit_old_ownership(monkeypatch):
    import services.patrol_supervisor as supervisor

    old = supervisor._ProcessIdentity(101, 2, 100, 100, 100, "S")
    reused = supervisor._ProcessIdentity(101, 3, 1, 101, 101, "S")
    monkeypatch.setattr(supervisor, "_process_table", lambda: {101: reused})
    known = {101: old}
    process = type("Process", (), {"pid": 100})()
    recovery_health._observe_owned_process(process, known)
    assert known[101] == old


def test_owned_executor_rejects_inmemory_delivery():
    with pytest.raises(ValueError, match="RQ"):
        OwnedRecoveryExecutor({"SCRAPE_QUEUE_BACKEND": "inmemory"})


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux /proc ownership view unavailable")
def test_ownership_loss_stops_owned_child_promptly(monkeypatch):
    processes = []
    lost = threading.Event()
    real_popen = subprocess.Popen

    def hung_child(_args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        lost.set()
        return process

    monkeypatch.setattr(recovery_health.subprocess, "Popen", hung_child)
    with pytest.raises(RecoveryCancelled):
        OwnedRecoveryExecutor({"SCRAPE_QUEUE_BACKEND": "rq"}, cancelled=lost.is_set)("detail", lambda: {}, 60)
    assert processes[0].poll() is not None


def test_cancellation_does_not_admit_later_phase():
    heartbeats = Heartbeats()
    calls = []
    result = run_recovery_phases({"detail": lambda: calls.append("unsafe")},
        heartbeat_writer=heartbeats.write, cancelled=lambda: True)
    assert calls == []
    assert result["features"]["detail"]["error_type"] == "RecoveryCancelled"


def test_exception_diagnostics_include_stage_without_message():
    try:
        try:
            raise ConnectionError("postgresql://owner:secret@db private SQL")
        except ConnectionError as cause:
            raise RuntimeError("statement and password must not be exported") from cause
    except RuntimeError as error:
        diagnostic = recovery_health.exception_diagnostic(error)
    assert diagnostic["error_chain"] == ["RuntimeError", "ConnectionError"]
    assert diagnostic["stack"][0]["file"] == "test_recovery_health.py"
    assert "secret" not in str(diagnostic)
    assert "postgresql" not in str(diagnostic)


def test_deadline_reaps_real_child_using_injected_process_view(monkeypatch):
    """Check control flow; this mocked view does not verify Linux descendants."""
    import services.patrol_supervisor as supervisor

    processes = []
    real_popen = subprocess.Popen

    def hung_child(_args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    def process_table():
        if not processes or processes[0].poll() is not None:
            return {}
        process = processes[0]
        return {process.pid: supervisor._ProcessIdentity(process.pid, 1, 1, process.pid, process.pid, "S")}

    monkeypatch.setattr(recovery_health.subprocess, "Popen", hung_child)
    monkeypatch.setattr(supervisor, "_process_table", process_table)
    with pytest.raises(RecoveryTimeout):
        OwnedRecoveryExecutor({"SCRAPE_QUEUE_BACKEND": "rq"})("detail", lambda: {}, 0.9)
    assert processes[0].poll() is not None
