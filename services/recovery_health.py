"""Per-feature recovery outcomes and owned-process execution for split workers.

Scheduler liveness is deliberately separate from business success. An empty
pass only proves the recovery scan returned no work; it is not evidence that a
thumbnail, notification, detail fetch or translation was delivered.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from services.operation_deadline import OperationDeadlineExceeded, deadline_scope


logger = logging.getLogger(__name__)
RECOVERY_FEATURES = ("notification", "thumbnail", "detail", "translation")
RECOVERY_HEALTHY_STATES = frozenset({"ok", "no_pending"})
DEFAULT_PHASE_TIMEOUT_SECONDS = 60.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 240.0
_COUNT_FIELDS = {
    "notification": ("considered", "enqueued", "failed"),
    "thumbnail": ("queued_batches", "queued_images", "failed"),
    "detail": ("queued", "skipped", "pending", "failed"),
    "translation": ("recovered", "enqueued", "failed"),
}

@dataclass
class _OwnedRecoveryChild:
    process: subprocess.Popen
    known: dict = field(default_factory=dict)


_unreaped_children: list[_OwnedRecoveryChild] = []
_recovery_admission_lock = threading.Lock()


class RecoveryTimeout(TimeoutError):
    """The phase's owned child has been stopped and reaped after its deadline."""


class RecoveryCleanupIncomplete(RuntimeError):
    """Child termination was not verified; further recovery admission stops."""


class RecoveryCancelled(RuntimeError):
    """Scheduler ownership was lost; no new recovery work may be admitted."""


class RecoveryPhaseError(RuntimeError):
    def __init__(self, error_type: str, diagnostic: Mapping | None = None):
        self.error_type = error_type
        self.diagnostic = _sanitize_diagnostic(diagnostic or {})
        super().__init__(error_type)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decode(payload: Mapping) -> dict[str, str]:
    return {
        key.decode() if isinstance(key, bytes) else str(key):
        value.decode() if isinstance(value, bytes) else str(value)
        for key, value in payload.items()
    }


def recovery_counts(feature: str, summary: Mapping) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in _COUNT_FIELDS[feature]:
        raw = summary.get(key, 0)
        if isinstance(raw, (list, tuple, set)):
            value = len(raw)
        elif isinstance(raw, bool):
            value = int(raw)
        else:
            value = int(raw or 0)
        if value < 0:
            raise ValueError("Recovery summary counts cannot be negative")
        counts[key] = value
    return counts


def _error_type(error: Exception) -> str:
    return getattr(error, "error_type", type(error).__name__)


def _sanitize_diagnostic(diagnostic: Mapping) -> dict[str, Any]:
    """Keep only class/frame identities, never exception messages or locals."""
    names = [name for name in diagnostic.get("error_chain", [])
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,100}", name)][:8]
    frames = []
    for frame in diagnostic.get("stack", [])[:24]:
        if not isinstance(frame, Mapping):
            continue
        file = str(frame.get("file", ""))
        function = str(frame.get("function", ""))
        line = frame.get("line")
        if (re.fullmatch(r"[A-Za-z_0-9.-]{1,120}", file)
            and re.fullmatch(r"[A-Za-z_0-9<>]{1,120}", function)
            and isinstance(line, int) and 0 < line < 1000000):
            frames.append({"file": file, "function": function, "line": line})
    return {"error_chain": names, "stack": frames}


def exception_diagnostic(error: BaseException) -> dict[str, Any]:
    names, frames, seen = [], [], set()
    current = error
    while current is not None and id(current) not in seen and len(names) < 8:
        seen.add(id(current))
        names.append(type(current).__name__)
        frame = current.__traceback__
        while frame is not None:
            frames.append({"file": Path(frame.tb_frame.f_code.co_filename).name,
                "function": frame.tb_frame.f_code.co_name, "line": frame.tb_lineno})
            frame = frame.tb_next
        current = current.__cause__ or current.__context__
    return _sanitize_diagnostic({"error_chain": names, "stack": frames[-24:]})


def run_recovery_phases(
    phases: Mapping[str, Callable[[], Mapping]],
    *,
    heartbeat_writer: Callable[..., None],
    executor: Callable[[str, Callable, float], Mapping] | None = None,
    phase_timeout_seconds: float = DEFAULT_PHASE_TIMEOUT_SECONDS,
    total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    phase_heartbeat_reserve_seconds: float = 0.0,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = _utc_now,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run sequential phases without masking an earlier business failure.

    RQ workers must supply ``OwnedRecoveryExecutor`` for hard wall-clock
    bounds. The synchronous fallback is for tests/legacy in-memory delivery;
    its DB connect/statement limits do not interrupt arbitrary Python code.
    No detached timeout threads are created in either mode.
    """
    if (not math.isfinite(phase_timeout_seconds) or not math.isfinite(total_timeout_seconds)
        or not math.isfinite(phase_heartbeat_reserve_seconds)
        or phase_timeout_seconds <= 0 or total_timeout_seconds <= 0
        or phase_heartbeat_reserve_seconds < 0):
        raise ValueError("Recovery deadlines must be positive")
    # The outer writer is capped at start+total. Keep one final heartbeat
    # window separate from per-phase completion windows so a saturated last
    # phase cannot consume the aggregate's entire budget.
    deadline = monotonic() + total_timeout_seconds - phase_heartbeat_reserve_seconds
    outcomes: dict[str, dict[str, Any]] = {}
    latest_feature_fields: dict[str, Any] = {}

    def phase_writer(**fields: Any) -> None:
        # Always collect the latest outcomes, even when this phase's metadata
        # window is exhausted. The reserved aggregate writes them atomically.
        latest_feature_fields.update({name: value for name, value in fields.items()
            if name.startswith("last_recovery_")})
        if phase_heartbeat_reserve_seconds == 0:
            heartbeat_writer(**fields)
            return
        if monotonic() >= deadline:
            return
        try:
            with deadline_scope(deadline):
                heartbeat_writer(**fields)
        except OperationDeadlineExceeded:
            # A skipped metadata write cannot consume the aggregate reserve.
            return

    cleanup_failed = False
    for feature, operation in phases.items():
        if feature not in RECOVERY_FEATURES:
            raise ValueError("Unknown recovery feature")
        prefix = f"last_recovery_{feature}_"
        started = monotonic()
        started_at = utc_now().isoformat()
        allowance = min(phase_timeout_seconds, deadline - started)
        phase_writer(
            event="recovery_phase_started",
            **{prefix + "started_at": started_at,
               prefix + "status": "running",
               prefix + "timeout_seconds": max(0.0, allowance),
               prefix + "error": ""},
        )
        try:
            now = monotonic()
            allowance = min(
                phase_timeout_seconds - (now - started) - phase_heartbeat_reserve_seconds,
                deadline - now - phase_heartbeat_reserve_seconds,
            )
            if cancelled is not None and cancelled():
                raise RecoveryCancelled("Recovery scheduler ownership was lost")
            if cleanup_failed:
                raise RecoveryCleanupIncomplete("Earlier phase still owns resources")
            if allowance <= 0:
                raise RecoveryTimeout("Recovery batch deadline reached")
            summary = operation() if executor is None else executor(feature, operation, allowance)
            if not isinstance(summary, Mapping):
                raise TypeError("Recovery phase did not return a summary")
            counts = recovery_counts(feature, summary)
            failed = counts.get("failed", 0)
            observed = sum(value for key, value in counts.items() if key != "failed")
            status = "degraded" if failed else "no_pending" if observed == 0 else "ok"
            finished_at = utc_now().isoformat()
            fields = {
                prefix + "finished_at": finished_at,
                prefix + "status": status,
                prefix + "duration_seconds": round(monotonic() - started, 6),
                prefix + "error": "RecoveryEnqueueFailure" if failed else "",
                prefix + "diagnostic": {},
                prefix + "no_pending_count": int(status == "no_pending"),
                **{prefix + key + "_count": value for key, value in counts.items()},
            }
            if failed:
                fields[prefix + "failed_at"] = finished_at
            else:
                # Only a successful scan updates completed_at. Failure and
                # running transitions retain the previous successful timestamp.
                fields[prefix + "completed_at"] = finished_at
            phase_writer(event="recovery_phase_completed", **fields)
            outcomes[feature] = {"status": status, "summary": dict(summary), "counts": counts}
        except Exception as exc:
            cleanup_failed = cleanup_failed or isinstance(exc, RecoveryCleanupIncomplete)
            error = _error_type(exc)
            diagnostic = getattr(exc, "diagnostic", exception_diagnostic(exc))
            status = "quarantined" if cleanup_failed else "failed"
            phase_writer(
                event="recovery_phase_failed",
                **{prefix + "finished_at": utc_now().isoformat(),
                   prefix + "failed_at": utc_now().isoformat(),
                   prefix + "status": status,
                   prefix + "duration_seconds": round(monotonic() - started, 6),
                   prefix + "error": error,
                   prefix + "diagnostic": diagnostic,
                   prefix + "failed_count": 1,
                   prefix + "no_pending_count": 0},
            )
            logger.warning("Recovery phase failed: feature=%s error_type=%s diagnostic=%s", feature, error, diagnostic)
            outcomes[feature] = {"status": status, "error_type": error, "diagnostic": diagnostic}
    aggregate = "ok" if all(item["status"] in RECOVERY_HEALTHY_STATES for item in outcomes.values()) else "degraded"
    heartbeat_writer(
        event="recovery_batch_completed",
        **latest_feature_fields,
        last_recovery_completed_at=utc_now().isoformat(),
        last_recovery_status=aggregate,
        last_recovery_failed_features=[feature for feature, result in outcomes.items() if result["status"] not in RECOVERY_HEALTHY_STATES],
    )
    return {"status": aggregate, "features": outcomes}


def inspect_recovery_heartbeats(
    connection: Any,
    *,
    key: str,
    freshness_seconds: int = 900,
    now: datetime | None = None,
) -> dict[str, str]:
    """Inspect each feature independently of worker/scheduler liveness."""
    states = {f"recovery_{feature}": "unavailable" for feature in RECOVERY_FEATURES}
    try:
        payload = _decode(connection.hgetall(key) or {})
        if payload.get("runtime_role") != "worker":
            return states
        current_time = now or _utc_now()
        for feature in RECOVERY_FEATURES:
            prefix = f"last_recovery_{feature}_"
            status = payload.get(prefix + "status", "")
            finished = _timestamp(payload.get(prefix + "finished_at"))
            started = _timestamp(payload.get(prefix + "started_at"))
            if status == "running":
                if started is not None:
                    age = (current_time - started).total_seconds()
                    budget = float(payload.get(prefix + "timeout_seconds", "0"))
                    states[f"recovery_{feature}"] = "running" if -60 <= age <= budget + 15 else "stale"
                continue
            if finished is None:
                continue
            age = (current_time - finished).total_seconds()
            if not -60 <= age <= max(1, freshness_seconds):
                states[f"recovery_{feature}"] = "stale"
            elif status in RECOVERY_HEALTHY_STATES:
                failed_count = int(payload.get(prefix + "failed_count", "0"))
                states[f"recovery_{feature}"] = "degraded" if failed_count else status
            elif status in {"failed", "degraded", "quarantined"}:
                states[f"recovery_{feature}"] = status
            else:
                states[f"recovery_{feature}"] = "failed"
    except Exception as exc:
        logger.warning("Recovery heartbeat inspection failed: error_type=%s", type(exc).__name__)
    return states


def recovery_operation(feature: str) -> Callable[[], Mapping]:
    """Resolve a fixed, audited recovery entrypoint; never import arbitrary input."""
    if feature == "notification":
        from services.catalog_request_notifications import recover_request_notifications
        return lambda: recover_request_notifications(limit=20)
    if feature == "thumbnail":
        from services.product_thumbnail_jobs import recover_thumbnail_jobs
        return recover_thumbnail_jobs
    if feature == "detail":
        from services.product_detail_jobs import recover_product_detail_jobs
        return lambda: recover_product_detail_jobs(limit=100)
    if feature == "translation":
        from services.translator.suggestion_store import recover_expired_translation_suggestions
        return recover_expired_translation_suggestions
    raise ValueError("Unknown recovery feature")


class OwnedRecoveryExecutor:
    """Run only the named RQ recovery entrypoint in a separately owned process."""

    def __init__(self, app_config: Mapping, *, cancelled: Callable[[], bool] | None = None):
        # Pass only the queue configuration required by dispatchers. Secrets
        # stay in the inherited environment/private IPC, never in argv/logs.
        self.config = {key: app_config[key] for key in (
            "SCRAPE_QUEUE_BACKEND", "REDIS_URL", "SCRAPE_QUEUE_NAME", "MEDIA_QUEUE_NAME",
            "MEDIA_JOB_TIMEOUT_SECONDS", "MEDIA_JOB_RESULT_TTL_SECONDS", "MEDIA_JOB_FAILURE_TTL_SECONDS",
        ) if key in app_config}
        self.cancelled = cancelled
        if str(self.config.get("SCRAPE_QUEUE_BACKEND", "")).lower() != "rq":
            raise ValueError("Owned recovery execution requires the RQ backend")

    def __call__(self, feature: str, _operation: Callable, timeout_seconds: float) -> Mapping:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.cancelled is not None and self.cancelled():
                raise RecoveryCancelled("Recovery scheduler ownership was lost")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecoveryTimeout("Recovery process admission deadline reached")
            if _recovery_admission_lock.acquire(timeout=min(0.05, remaining)):
                break
        try:
            return self._execute(feature, deadline)
        finally:
            _recovery_admission_lock.release()

    def _execute(self, feature: str, deadline: float) -> Mapping:
        global _unreaped_children
        still_owned = []
        for child in _unreaped_children:
            if not _stop_owned_process(child.process, deadline, min(1.0, max(0.0, deadline - time.monotonic())), known=child.known):
                still_owned.append(child)
        _unreaped_children = still_owned
        if still_owned:
            raise RecoveryCleanupIncomplete("A previous child session still owns resources")
        timeout_seconds = deadline - time.monotonic()
        if timeout_seconds <= 0:
            raise RecoveryTimeout("Recovery admission deadline reached")
        if feature not in RECOVERY_FEATURES:
            raise ValueError("Unknown recovery feature")
        with tempfile.TemporaryDirectory(prefix="esp-recovery-") as directory:
            result_file = Path(directory) / "result.json"
            cleanup_reserve = min(15.0, timeout_seconds / 3)
            process = subprocess.Popen(
                [sys.executable, "-m", "services.recovery_health", "--child", feature, str(result_file), str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parent.parent), start_new_session=True,
            )
            owned = _OwnedRecoveryChild(process)
            try:
                work_deadline = deadline - cleanup_reserve
                first_poll = True
                while True:
                    _observe_owned_process(process, owned.known)
                    if self.cancelled is not None and self.cancelled():
                        raise RecoveryCancelled("Recovery scheduler ownership was lost")
                    remaining = work_deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, timeout_seconds)
                    try:
                        process.communicate(
                            json.dumps(self.config).encode() if first_poll else None,
                            timeout=min(0.05, remaining),
                        )
                        break
                    except subprocess.TimeoutExpired:
                        first_poll = False
            except subprocess.TimeoutExpired:
                if not _stop_owned_process(process, deadline, cleanup_reserve, known=owned.known):
                    _unreaped_children.append(owned)
                    raise RecoveryCleanupIncomplete("Recovery child termination was not verified")
                raise RecoveryTimeout("Recovery phase wall-clock deadline reached")
            except BaseException:
                if not _stop_owned_process(process, deadline, cleanup_reserve, known=owned.known):
                    _unreaped_children.append(owned)
                    raise RecoveryCleanupIncomplete("Interrupted recovery child termination was not verified")
                raise
            if not _stop_owned_process(process, deadline, cleanup_reserve, known=owned.known):
                _unreaped_children.append(owned)
                raise RecoveryCleanupIncomplete("Recovery descendants did not terminate")
            if not result_file.exists() or result_file.stat().st_size > 256 * 1024:
                raise RecoveryPhaseError("RecoveryChildFailed")
            result = json.loads(result_file.read_text())
            if result.get("error_type"):
                raise RecoveryPhaseError(str(result["error_type"]), result.get("diagnostic"))
            if process.returncode != 0:
                raise RecoveryPhaseError("RecoveryChildFailed")
            return result["summary"]


def _observe_owned_process(process: subprocess.Popen, known: dict) -> None:
    from services.patrol_supervisor import observe_owned_process

    observe_owned_process(process, known=known, session=process.pid)


def _stop_owned_process(process: subprocess.Popen, deadline: float, allowance: float, *, known: dict | None = None) -> bool:
    """Use the same ownership/descendant checks as the patrol supervisor."""
    from services.patrol_supervisor import (
        PatrolSupervisorQuarantined,
        terminate_owned_process,
    )

    try:
        terminate_owned_process(
            process, deadline=deadline, session=process.pid,
            known=known,
            cleanup_seconds=allowance * 2 / 3,
            kill_seconds=allowance / 3,
        )
        return True
    except PatrolSupervisorQuarantined:
        return False


def _watch_parent(parent_pid: int) -> None:
    """A lost parent must not leave its separately owned recovery child alive."""
    while os.getppid() == parent_pid:
        time.sleep(0.1)
    import signal

    os.killpg(os.getpid(), signal.SIGKILL)


def _child(feature: str, result_file: str, parent_pid: int) -> int:
    threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True).start()
    try:
        config = json.loads(sys.stdin.buffer.read(64 * 1024))
        if str(config.get("SCRAPE_QUEUE_BACKEND", "")).lower() != "rq":
            raise ValueError("Recovery child requires RQ")
        from app import create_app

        child_app = create_app(runtime_role="worker", config_overrides={
            **config, "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
            "ENABLE_SCHEDULER": False, "REGISTER_BLUEPRINTS": False,
            "REGISTER_CLI_COMMANDS": False,
        })
        with child_app.app_context():
            summary = dict(recovery_operation(feature)())
        result = {"summary": summary}
        code = 0
    except Exception as exc:
        result = {"error_type": type(exc).__name__, "diagnostic": exception_diagnostic(exc)}
        code = 1
    Path(result_file).write_text(json.dumps(result))
    return code


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--child" or sys.argv[2] not in RECOVERY_FEATURES:
        raise SystemExit(2)
    raise SystemExit(_child(sys.argv[2], sys.argv[3], int(sys.argv[4])))
