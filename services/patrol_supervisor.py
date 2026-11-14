"""Owned patrol subprocesses with a verified resource-stop boundary.

The subprocess is spawned (never forked from an APScheduler thread), owns a
separate Linux session and reuses browser runtimes. A timed-out RPC is never
accepted later. Its process group and tracked descendants must be gone before
the supervisor permits another request. The worker parent performs no patrol
SQL; SupervisedPatrolRunner owns business writes inside the stoppable child.
"""
from __future__ import annotations

import atexit
import ctypes
from dataclasses import dataclass
import importlib
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
import uuid

from services.operation_deadline import OperationDeadlineExceeded, deadline_scope

logger = logging.getLogger("patrol.supervisor")
_MAX_MESSAGE_BYTES = 256 * 1024
_MAX_HYSTERESIS_PRODUCTS = 2048
_POLL_SECONDS = 0.05


class PatrolCancelled(RuntimeError):
    pass


class PatrolSupervisorQuarantined(RuntimeError):
    """Cannot prove the previous resource owner stopped: no new admission."""


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    started: int
    parent: int
    group: int
    session: int
    state: str


def _process_table() -> dict[int, _ProcessIdentity]:
    # Treat an unreadable /proc as an ownership-verification failure, rather
    # than interpreting an empty snapshot as proof that everything exited.
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        # A process name can itself contain spaces and parentheses.
        fields = stat[stat.rfind(")") + 2:].split()
        table[int(entry.name)] = _ProcessIdentity(
            int(entry.name), int(fields[19]), int(fields[1]),
            int(fields[2]), int(fields[3]), fields[0],
        )
    return table


def _owned_processes(table, root, known, session=None):
    session_valid = not (root in known and root in table and known[root].started != table[root].started)
    owned = {
        pid: identity for pid, identity in table.items()
        if (pid in known and known[pid].started == identity.started)
        or (session_valid and session is not None and identity.session == session)
    }
    if (root in table and (root not in known or table[root].started == known[root].started)
            and (session is None or table[root].session == session)):
        owned[root] = table[root]
    while True:
        additions = {pid: identity for pid, identity in table.items() if identity.parent in owned and pid not in owned}
        if not additions:
            break
        owned.update(additions)
    return owned


def _signal_identity(identity, sig):
    # Open first, then verify start-time. The descriptor pins the process even
    # if its numeric PID gets reused between verification and signalling.
    try:
        descriptor = os.pidfd_open(identity.pid, 0)
    except ProcessLookupError:
        return
    try:
        current = _process_table().get(identity.pid)
        if current is not None and current.started == identity.started:
            try:
                signal.pidfd_send_signal(descriptor, sig)
            except ProcessLookupError:
                pass
    finally:
        os.close(descriptor)


def observe_owned_process(process, *, known=None, session=None):
    known = known if known is not None else {}
    try:
        owned = _owned_processes(_process_table(), process.pid, known, session)
        known.update(owned)
        return owned
    except Exception as exc:
        raise PatrolSupervisorQuarantined("Owned process visibility was lost") from exc


def terminate_owned_process(process, *, deadline, cleanup_seconds=10.0,
                            kill_seconds=5.0, known=None, session=None):
    """Stop and verify an owned Linux session, usable by other job owners.

    ``session`` may only be supplied for a child created with setsid or
    start_new_session=True. ``known`` retains PID/start-time identities so
    reparenting cannot make an observed descendant disappear from ownership.
    """
    known = known if known is not None else {}

    def observe():
        owned = _owned_processes(_process_table(), process.pid, known, session)
        known.update(owned)
        return owned

    def reap():
        if hasattr(process, "join"):
            process.join(timeout=0)
        else:
            process.poll()

    try:
        owned = observe()
        soft_end = min(time.monotonic() + cleanup_seconds, deadline - kill_seconds)
        # Descendants first: the root can reap children before it exits.
        for identity in sorted(owned.values(), key=lambda item: item.pid == process.pid):
            _signal_identity(identity, signal.SIGTERM)
        while time.monotonic() < soft_end:
            reap()
            if not observe():
                return
            time.sleep(min(_POLL_SECONDS, max(0, soft_end - time.monotonic())))
        while time.monotonic() < deadline:
            reap()
            owned = observe()
            if not owned:
                return
            for identity in sorted(owned.values(), key=lambda item: item.pid == process.pid):
                _signal_identity(identity, signal.SIGKILL)
            time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
        reap()
        if observe():
            raise PatrolSupervisorQuarantined("Owned process termination was not verified")
    except PatrolSupervisorQuarantined:
        raise
    except Exception as exc:
        # Loss of visibility is never success. Still stop the independently
        # owned session as a best effort, then retain quarantine for anything
        # that cannot be verified (including a detached descendant).
        leader_alive = process.is_alive() if hasattr(process, "is_alive") else process.poll() is None
        if session is not None and leader_alive:
            try:
                os.killpg(session, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                logger.error("Unable to signal owned process session")
        try:
            if hasattr(process, "is_alive"):
                if process.is_alive():
                    process.kill()
                process.join(timeout=max(0, min(0.1, deadline - time.monotonic())))
            elif process.poll() is None:
                process.kill()
                process.wait(timeout=max(0.001, min(0.1, deadline - time.monotonic())))
        except Exception:
            logger.error("Unable to reap owned process after visibility loss")
        raise PatrolSupervisorQuarantined("Owned process visibility was lost") from exc


def _fetch_patrol(site, url):
    modules = {
        "mercari": ("mercari_patrol", "MercariPatrol"),
        "yahoo": ("yahoo_patrol", "YahooPatrol"),
        "rakuma": ("rakuma_patrol", "RakumaPatrol"),
        "surugaya": ("surugaya_patrol", "SurugayaPatrol"),
        "offmall": ("offmall_patrol", "OffmallPatrol"),
        "yahuoku": ("yahuoku_patrol", "YahuokuPatrol"),
        "snkrdunk": ("snkrdunk_patrol", "SnkrdunkPatrol"),
        "recordcity": ("recordcity_patrol", "RecordCityPatrol"),
    }
    module_name, class_name = modules[site]
    module = importlib.import_module("services.patrol." + module_name)
    return getattr(module, class_name)().fetch(url)


def _write_atomic_message(path, message):
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise ValueError("Patrol response exceeded IPC size limit")
    path = Path(path)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(payload)
    os.replace(temporary, path)


def _bounded_soft_sold_counts(counts):
    """Carry confirmed in-memory hysteresis across deliberate child retirement.

    Invalid or excess entries reset safely to no soft-sold evidence; retaining
    this small scalar map does not retain browser/response/product objects.
    """
    if not isinstance(counts, dict):
        return {}
    bounded = {}
    for key, value in list(counts.items())[-_MAX_HYSTERESIS_PRODUCTS:]:
        if not ((isinstance(key, int) and not isinstance(key, bool))
                or (isinstance(key, str) and key.isascii() and key.isdigit())):
            continue
        try:
            product_id = int(key)
        except (TypeError, ValueError, OverflowError):
            continue
        if (0 < product_id < 2 ** 63 and isinstance(value, int) and not isinstance(value, bool)
                and 0 < value <= 2):
            bounded[product_id] = value
    return bounded


def _read_atomic_message(path):
    try:
        with Path(path).open("rb") as handle:
            payload = handle.read(_MAX_MESSAGE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise ValueError("Patrol response exceeded IPC size limit")
    return json.loads(payload)


def _wait_child_request(directory):
    path = Path(directory) / "request.json"
    while True:
        message = _read_atomic_message(path)
        if message is not None:
            path.unlink()
            return message
        time.sleep(0.02)


def capture_owned_memory():
    """Sanitized milestone sample, executed only inside the bounded child."""
    table = _process_table()
    owned = _owned_processes(table, os.getpid(), {}, os.getpid())
    rows = []
    for identity in list(owned.values())[:128]:
        try:
            directory = Path("/proc") / str(identity.pid)
            name = (directory / "comm").read_text().strip().lower()
            category = ("python" if name.startswith("python") else "node" if name == "node"
                        else "chromium" if name.startswith(("chrome", "chromium")) else "other")
            status = {}
            for line in (directory / "status").read_text().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    if key in {"VmRSS", "Threads"}:
                        status[key] = int(value.strip().split()[0])
            pss = None
            try:
                for line in (directory / "smaps_rollup").read_text().splitlines():
                    if line.startswith("Pss:"):
                        pss = int(line.split()[1]) * 1024
                        break
            except (FileNotFoundError, PermissionError):
                pass
            rows.append({"pid": identity.pid, "ppid": identity.parent, "started": identity.started,
                         "class": category, "rss_bytes": status.get("VmRSS", 0) * 1024,
                         "pss_bytes": pss, "threads": status.get("Threads")})
        except FileNotFoundError:
            continue
    return {"processes": rows, "owned_process_count": len(owned), "truncated": len(owned) > 128}


def _parent_watchdog(parent_pid, session):
    # A disconnected parent must not leave an idle driver/browser generation
    # alive. No environment, command lines or credentials are inspected.
    while os.getppid() == parent_pid:
        time.sleep(0.25)
    try:
        table = _process_table()
        owned = _owned_processes(table, os.getpid(), {}, session)
        for pid, identity in owned.items():
            if pid != os.getpid():
                _signal_identity(identity, signal.SIGKILL)
    finally:
        os.killpg(session, signal.SIGKILL)


def _child_shutdown(signum, frame):
    """Reap browser grandchildren before their owning supervisor child exits."""
    until = time.monotonic() + 2.0
    known = {}
    while time.monotonic() < until:
        table = _process_table()
        owned = _owned_processes(table, os.getpid(), known, os.getpid())
        known.update(owned)
        children = [identity for pid, identity in owned.items() if pid != os.getpid()]
        for identity in children:
            _signal_identity(identity, signal.SIGKILL)
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        if not children:
            raise SystemExit(0)
        time.sleep(0.01)
    raise SystemExit(1)


def _setup_child(parent_pid, profile_directory):
    if os.getsid(0) != os.getpid():
        os.setsid()
    session = os.getpid()
    os.environ["RECORDCITY_PERSISTENT_PROFILE_DIR"] = str(Path(profile_directory) / "chrome")
    # Adopt browser grandchildren locally if their Node parent crashes. This
    # applies only to this new child, not the worker's RQ/subprocess handling.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Unable to own browser descendants")
    signal.signal(signal.SIGTERM, _child_shutdown)
    threading.Thread(target=_parent_watchdog, args=(parent_pid, session), daemon=True).start()


def _patrol_child_main(parent_pid, generation, profile_directory, fetch_callable):
    _setup_child(parent_pid, profile_directory)
    # The fetch-only compatibility child leaves evidence delivery to its
    # parent. The whole-monitor child owns reporting and retains these URLs.
    for name in ("SCRAPE_ALERT_WEBHOOK_URL", "SELECTOR_ALERT_WEBHOOK_URL", "OPERATIONAL_ALERT_WEBHOOK_URL"):
        os.environ.pop(name, None)
    output_path = Path(profile_directory) / "result.json"
    _write_atomic_message(output_path, {"kind": "ready", "generation": generation, "pid": os.getpid()})
    while True:
        request = _wait_child_request(profile_directory)
        if request.get("kind") == "stop":
            return
        request_id = request["request_id"]
        try:
            from services.marketplace_access import marketplace_process_generation, has_marketplace_generation_quarantine
            from services.patrol.base_patrol import suppress_patrol_reporting
            with marketplace_process_generation(generation), suppress_patrol_reporting(), deadline_scope(request["deadline"]):
                result = fetch_callable(request["site"], request["url"])
            from services.browser_pool import get_browser_pool_health
            runtimes = get_browser_pool_health()["runtimes"].values()
            uncertain = any(runtime.get("quarantined") or runtime.get("active_tasks", 0) for runtime in runtimes)
            uncertain = uncertain or has_marketplace_generation_quarantine(generation)
            fields = ("price", "status", "variants", "error", "confidence", "reason", "price_source", "evidence_strength")
            _write_atomic_message(output_path, {
                "kind": "result", "generation": generation, "request_id": request_id,
                "result": {field: getattr(result, field, None) for field in fields},
                "retire": uncertain,
            })
        except Exception as exc:
            # Raw exception messages can contain authenticated target URLs.
            _write_atomic_message(output_path, {
                "kind": "error", "generation": generation, "request_id": request_id,
                "error": type(exc).__name__,
            })


def _patrol_batch_child_main(parent_pid, generation, directory):
    _setup_child(parent_pid, directory)
    output_path = Path(directory) / "result.json"
    progress_path = Path(directory) / "progress.json"
    _write_atomic_message(output_path, {"kind": "ready", "generation": generation, "pid": os.getpid()})
    while True:
        request = _wait_child_request(directory)
        if request.get("kind") == "stop":
            return
        request_id = request["request_id"]
        try:
            from services.marketplace_access import marketplace_process_generation
            from services.monitor_service import MonitorService
            from services.patrol_ownership import patrol_ownership
            from services.marketplace_access import has_marketplace_generation_quarantine
            from services.browser_pool import get_browser_pool_health
            def resource_guard():
                runtimes = get_browser_pool_health()["runtimes"].values()
                if (has_marketplace_generation_quarantine(generation)
                        or any(runtime.get("quarantined") or runtime.get("active_tasks", 0) for runtime in runtimes)):
                    raise PatrolSupervisorQuarantined("A browser operation did not stop cleanly")
            state = {"all_selected_ids": [], "finished_ids": [], "active_item": None,
                     "active_deadline": None, "active_site": None, "touched_sites": []}
            def progress(message):
                if message["event"] == "selected":
                    state["all_selected_ids"] = message["selected_ids"]
                elif message["event"] == "item_started":
                    state["active_item"] = message["product_id"]
                    state["active_deadline"] = message["deadline"]
                    state["active_site"] = message["site"]
                    if message["site"] not in state["touched_sites"]:
                        state["touched_sites"].append(message["site"])
                elif message["event"] == "item_finished":
                    state["finished_ids"].append(message["product_id"])
                    state["active_item"], state["active_deadline"] = None, None
                    state["active_site"] = None
                if (message["event"] == "selected" or (message["event"] == "item_finished" and
                        (len(state["finished_ids"]) % 10 == 0 or len(state["finished_ids"]) == len(state["all_selected_ids"])))):
                    try:
                        state["memory_sample"] = capture_owned_memory()
                        logger.info("Patrol owned memory sample generation=%s sample=%s", generation,
                                    json.dumps(state["memory_sample"], separators=(",", ":")))
                    except OSError:
                        state["memory_sample"] = {"unavailable": True}
                _write_atomic_message(progress_path, dict(message, **state, generation=generation, request_id=request_id))
            with marketplace_process_generation(generation), patrol_ownership(request.get("scheduler_ownership")) as ownership_guard:
                try:
                    MonitorService._mercari_soft_sold_counts = _bounded_soft_sold_counts(request.get("soft_sold_counts"))
                    summary = MonitorService.check_stale_products(
                        limit=request["limit"], deadline=request["deadline"],
                        item_timeout=request["item_timeout"], progress=progress,
                        checkpoint_attempts=True, product_ids=request.get("product_ids"),
                        ownership_guard=ownership_guard,
                        session_factory=ownership_guard.session_factory,
                        resource_guard=resource_guard,
                        cancelled=lambda: os.getppid() != parent_pid or (Path(directory) / "cancel").exists(),
                    )
                    response = {
                        "kind": "batch_result", "generation": generation,
                        "request_id": request_id, "summary": summary, "retire": True,
                        "soft_sold_counts": _bounded_soft_sold_counts(MonitorService._mercari_soft_sold_counts)
                        if summary.get("status") in {"completed", "no_products"} else {},
                    }
                except Exception as exc:
                    # Even reporting/persistence errors retain the physical
                    # fence until owned browsers have been stopped by parent.
                    response = {
                        "kind": "error", "generation": generation,
                        "request_id": request_id, "error": type(exc).__name__,
                    }
                try:
                    _write_atomic_message(output_path, response)
                except Exception as exc:
                    # IPC disk/serialization failure cannot release the fence
                    # while the generation's browsers remain alive either.
                    logger.error("Unable to write owned patrol result: %s", type(exc).__name__)
                    try:
                        _write_atomic_message(output_path, {
                            "kind": "error", "generation": generation,
                            "request_id": request_id, "error": type(exc).__name__,
                        })
                    except Exception as reporting_exc:
                        logger.error("Unable to write owned patrol error: %s", type(reporting_exc).__name__)
                # Keep the same-connection advisory fence until the parent
                # stops this generation. No idle duplicate browser survives a
                # completed batch, and successor admission follows old stop.
                while True:
                    time.sleep(0.1)
        except Exception as exc:
            _write_atomic_message(output_path, {
                "kind": "error", "generation": generation, "request_id": request_id,
                "error": type(exc).__name__,
            })


def _clear_generation_bounded(generation, sites, directory, deadline, *, quarantine=False):
    if not sites:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        raise PatrolSupervisorQuarantined("No quarantine cleanup budget remains")
    output_path = Path(directory) / "clear-result.json"
    output_path.unlink(missing_ok=True)
    work_deadline = deadline - min(0.5, remaining / 3)
    process = subprocess.Popen([
        sys.executable, "-m", "services.patrol_process",
        "--quarantine-generation" if quarantine else "--clear-generation", generation,
        "--sites", ",".join(sorted(sites)), "--deadline", str(work_deadline), "--result", str(output_path),
    ], start_new_session=True)
    known = {}
    try:
        while time.monotonic() < work_deadline:
            observe_owned_process(process, known=known, session=process.pid)
            if process.poll() is not None:
                result = _read_atomic_message(output_path)
                if process.returncode == 0 and result == {"quarantined" if quarantine else "cleared": True}:
                    return
                raise PatrolSupervisorQuarantined("Generation quarantine cleanup failed")
            time.sleep(min(_POLL_SECONDS, max(0, work_deadline - time.monotonic())))
        raise PatrolSupervisorQuarantined("Generation quarantine cleanup timed out")
    finally:
        terminate_owned_process(process, deadline=deadline, cleanup_seconds=0,
                                kill_seconds=max(0, deadline - time.monotonic()), known=known, session=process.pid)


def _setting(name, default, minimum, maximum):
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) and minimum <= value <= maximum else default
    except (TypeError, ValueError):
        return default


def patrol_item_timeout():
    return _setting("PATROL_ITEM_TIMEOUT_SECONDS", 120.0, 30.0, 300.0)


def patrol_batch_timeout():
    return _setting("PATROL_BATCH_TIMEOUT_SECONDS", 780.0, 60.0, 840.0)


def patrol_cleanup_budget():
    """Root lease cutoff must cover the same validated stop settings."""
    return (_setting("PATROL_CLEANUP_TIMEOUT_SECONDS", 10.0, 1.0, 20.0)
            + _setting("PATROL_KILL_TIMEOUT_SECONDS", 5.0, 1.0, 10.0))


class PatrolSupervisor:
    def __init__(self, *, fetch_callable=_fetch_patrol, cleanup_seconds=None, kill_seconds=None,
                 max_tasks=None, max_runtime_seconds=None):
        if (os.name != "posix" or not Path("/proc").is_dir()
                or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal")):
            raise PatrolSupervisorQuarantined("Patrol requires Linux process ownership verification")
        try:
            descriptor = os.pidfd_open(os.getpid(), 0)
            os.close(descriptor)
        except OSError as exc:
            raise PatrolSupervisorQuarantined("Patrol requires kernel pidfd support") from exc
        self._context = multiprocessing.get_context("spawn")
        self._fetch_callable = fetch_callable
        self._cleanup_seconds = cleanup_seconds if cleanup_seconds is not None else _setting("PATROL_CLEANUP_TIMEOUT_SECONDS", 10.0, 1.0, 20.0)
        self._kill_seconds = kill_seconds if kill_seconds is not None else _setting("PATROL_KILL_TIMEOUT_SECONDS", 5.0, 1.0, 10.0)
        self._max_tasks = max_tasks if max_tasks is not None else _setting("PATROL_PROCESS_MAX_TASKS", 50, 1, 500)
        self._max_runtime = max_runtime_seconds if max_runtime_seconds is not None else _setting("PATROL_PROCESS_MAX_RUNTIME_SECONDS", 900.0, 60.0, 3600.0)
        self._process = None
        self._connection = None
        self._generation = None
        self._retiring_generation = None
        self._known = {}
        self._session = None
        self._started_at = None
        self._profile_directory = None
        self._tasks = 0
        self._sites = set()
        self._soft_sold_counts = {}
        self._quarantined = False
        self._lock = threading.Lock()

    @property
    def quarantined(self):
        return self._quarantined

    def _observe(self):
        if self._process is None:
            return {}
        table = _process_table()
        owned = _owned_processes(table, self._process.pid, self._known, self._session)
        self._known.update(owned)
        return owned

    def _wait_message(self, deadline, cancelled):
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                raise PatrolCancelled("Patrol ownership was lost")
            self._observe()
            message = _read_atomic_message(Path(self._profile_directory) / "result.json")
            if message is not None:
                (Path(self._profile_directory) / "result.json").unlink()
                return message
            alive = self._process.is_alive() if hasattr(self._process, "is_alive") else self._process.poll() is None
            if not alive:
                raise RuntimeError("Patrol fetch process exited")
            time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
        raise OperationDeadlineExceeded("Patrol fetch deadline exceeded")

    def _start(self, deadline, cancelled):
        generation = uuid.uuid4().hex
        profile_directory = tempfile.mkdtemp(prefix="esp-patrol-" + generation + "-")
        process = self._context.Process(
            target=_patrol_child_main,
            args=(os.getpid(), generation, profile_directory, self._fetch_callable),
            name="esp-patrol-fetch",
        )
        self._process, self._connection, self._generation = process, None, generation
        self._profile_directory = profile_directory
        self._retiring_generation = None
        self._known, self._session = {}, None
        self._sites = set()
        process.start()
        self._observe()
        ready = self._wait_message(deadline, cancelled)
        identity = _process_table().get(process.pid)
        if (ready != {"kind": "ready", "generation": generation, "pid": process.pid}
                or identity is None or identity.group != process.pid or identity.session != process.pid):
            raise PatrolSupervisorQuarantined("Patrol child ownership could not be verified")
        self._session = process.pid
        self._started_at, self._tasks = time.monotonic(), 0
        logger.info("Patrol process ready generation=%s pid=%s", generation, process.pid)

    def _retire(self, deadline):
        """Bounded TERM -> KILL -> verify, retaining state on any uncertainty."""
        if self._process is None:
            return
        generation = self._generation or self._retiring_generation
        try:
            # Invalidate RPC acceptance before sending any stop signal.
            self._retiring_generation = generation
            self._generation = None
            try:
                if self._sites:
                    remaining = max(0, deadline - time.monotonic())
                    fence_deadline = min(deadline, time.monotonic() + min(3.0, remaining / 3))
                    _clear_generation_bounded(generation, self._sites, self._profile_directory,
                                              fence_deadline, quarantine=True)
            except Exception:
                # Stop is mandatory even if the fence backend is unreachable.
                # Unverified stopping retains local quarantine below.
                logger.error("Unable to persist retirement fence within its budget")
            terminate_owned_process(
                self._process, deadline=deadline - min(3.0, max(0.1, (deadline - time.monotonic()) / 5)),
                cleanup_seconds=self._cleanup_seconds,
                kill_seconds=self._kill_seconds, known=self._known, session=self._session,
            )
            # Token-scoped clear never touches another generation's lease.
            # Redis DNS/connect/native waits also live behind an owned process
            # boundary. Socket timeouts alone are not a whole-cleanup deadline.
            _clear_generation_bounded(generation, self._sites, self._profile_directory, deadline)
            if self._profile_directory is not None:
                shutil.rmtree(self._profile_directory)
                self._profile_directory = None
            logger.info("Patrol process stopped and verified generation=%s tracked_processes=%s",
                        generation, len(self._known))
        except Exception as exc:
            self._quarantined = True
            if isinstance(exc, PatrolSupervisorQuarantined):
                raise
            raise PatrolSupervisorQuarantined("Patrol ownership verification failed") from exc
        if self._connection is not None:
            self._connection.close()
        if hasattr(self._process, "close"):
            self._process.close()
        self._process, self._connection = None, None
        self._known, self._session, self._started_at, self._tasks = {}, None, None, 0
        self._retiring_generation = None

    def fetch(self, site, url, *, deadline=None, cancelled=None):
        deadline = deadline if deadline is not None else time.monotonic() + patrol_item_timeout()
        with self._lock:
            if self._quarantined:
                raise PatrolSupervisorQuarantined("Previous patrol resource owner is quarantined")
            work_deadline = deadline - self._cleanup_seconds - self._kill_seconds
            try:
                if cancelled is not None and cancelled():
                    raise PatrolCancelled("Patrol ownership was lost")
                if self._process is not None and (
                    self._tasks >= self._max_tasks or time.monotonic() - self._started_at >= self._max_runtime
                ):
                    self._retire(min(deadline, time.monotonic() + self._cleanup_seconds + self._kill_seconds))
                if time.monotonic() >= work_deadline:
                    raise OperationDeadlineExceeded("No fetch budget remains")
                if self._process is None:
                    self._start(work_deadline, cancelled)
                request_id = uuid.uuid4().hex
                self._sites.add(site)
                started = time.monotonic()
                logger.info("Patrol fetch start generation=%s request=%s site=%s remaining_seconds=%.3f",
                            self._generation, request_id, site, work_deadline - started)
                _write_atomic_message(Path(self._profile_directory) / "request.json", {
                    "kind": "fetch", "generation": self._generation, "request_id": request_id,
                    "site": site, "url": url, "deadline": work_deadline,
                })
                response = self._wait_message(work_deadline, cancelled)
                if (response.get("generation") != self._generation or response.get("request_id") != request_id
                        or response.get("kind") not in {"result", "error"}):
                    raise PatrolSupervisorQuarantined("Stale or invalid patrol response")
                if cancelled is not None and cancelled():
                    raise PatrolCancelled("Patrol ownership was lost")
                if time.monotonic() >= work_deadline:
                    raise OperationDeadlineExceeded("Patrol result arrived after deadline")
                self._tasks += 1
                logger.info("Patrol fetch result generation=%s request=%s site=%s duration_seconds=%.3f",
                            self._generation, request_id, site, time.monotonic() - started)
                if response["kind"] == "error":
                    raise RuntimeError("Patrol child failed: " + response["error"])
                from services.patrol.base_patrol import PatrolResult
                if response.get("retire"):
                    self._retire(min(deadline, time.monotonic() + self._cleanup_seconds + self._kill_seconds))
                return PatrolResult(**response["result"])
            except BaseException:
                # Includes KeyboardInterrupt/cancellation: no old operation
                # may retain resources behind a returned timeout exception.
                self._retire(min(deadline, time.monotonic() + self._cleanup_seconds + self._kill_seconds))
                raise

    def close(self):
        with self._lock:
            self._retire(time.monotonic() + self._cleanup_seconds + self._kill_seconds)


class SupervisedPatrolRunner(PatrolSupervisor):
    """The hard boundary includes selection, DB persistence and observation.

    One executable child owns MonitorService and its browsers. The worker
    parent executes no patrol SQL, so driver/DNS/COMMIT stalls are stoppable.
    """

    def _accept_batch_result(self, result):
        self._batch_requires_retire = result.get("retire", False)
        summary = result["summary"]
        self._soft_sold_counts = (_bounded_soft_sold_counts(result.get("soft_sold_counts"))
                                 if summary.get("status") in {"completed", "no_products"} else {})
        return summary

    def _start(self, deadline, cancelled):
        generation = uuid.uuid4().hex
        directory = tempfile.mkdtemp(prefix="esp-patrol-" + generation + "-")
        process = subprocess.Popen([
            sys.executable, "-m", "services.patrol_process",
            "--parent-pid", str(os.getpid()),
            "--generation", generation, "--directory", directory,
        ], start_new_session=True)
        self._process, self._connection, self._generation = process, None, generation
        self._profile_directory, self._retiring_generation = directory, None
        self._known, self._session, self._sites = {}, process.pid, set()
        ready = self._wait_message(deadline, cancelled)
        identity = _process_table().get(process.pid)
        if (ready != {"kind": "ready", "generation": generation, "pid": process.pid}
                or identity is None or identity.session != process.pid or identity.group != process.pid):
            raise PatrolSupervisorQuarantined("Patrol process ownership could not be verified")
        self._started_at, self._tasks = time.monotonic(), 0
        logger.info("Patrol monitor process ready generation=%s pid=%s", generation, process.pid)

    def _wait_batch(self, request_id, deadline, cancelled, ownership_deadline):
        item_deadline = deadline
        active_item = None
        seen_progress = None
        while time.monotonic() < min(deadline, item_deadline):
            now = time.monotonic()
            if ((cancelled is not None and cancelled())
                    or (ownership_deadline is not None and now >= ownership_deadline())):
                (Path(self._profile_directory) / "cancel").touch()
                raise PatrolCancelled("Scheduler ownership ended")
            self._observe()
            progress = _read_atomic_message(Path(self._profile_directory) / "progress.json")
            if progress is not None and progress != seen_progress:
                if progress.get("generation") != self._generation or progress.get("request_id") != request_id:
                    raise PatrolSupervisorQuarantined("Stale patrol progress")
                seen_progress = progress
                self._last_progress = progress
                self._selected_ids = progress["all_selected_ids"]
                self._finished_ids = set(progress["finished_ids"])
                self._sites.update(progress["touched_sites"])
                active_item = progress["active_item"]
                item_deadline = progress["active_deadline"] or deadline
            result = _read_atomic_message(Path(self._profile_directory) / "result.json")
            if result is not None:
                (Path(self._profile_directory) / "result.json").unlink()
                if result.get("generation") != self._generation or result.get("request_id") != request_id:
                    raise PatrolSupervisorQuarantined("Stale patrol batch result")
                if result["kind"] == "batch_result":
                    return self._accept_batch_result(result)
                raise RuntimeError("Patrol monitor failed: " + result.get("error", "invalid_result"))
            if self._process.poll() is not None:
                raise RuntimeError("Patrol monitor process exited")
            time.sleep(min(_POLL_SECONDS, max(0, min(deadline, item_deadline) - time.monotonic())))
        self._timed_out_item = active_item
        raise OperationDeadlineExceeded("Whole patrol item or batch deadline exceeded")

    def run_batch(self, *, limit=50, deadline=None, item_timeout=None,
                  cancelled=None, ownership_deadline=None, scheduler_ownership=None):
        deadline = deadline if deadline is not None else time.monotonic() + patrol_batch_timeout()
        item_timeout = item_timeout if item_timeout is not None else patrol_item_timeout()
        reserve = self._cleanup_seconds + self._kill_seconds
        if not math.isfinite(deadline) or not math.isfinite(item_timeout) or item_timeout <= reserve + 1:
            raise ValueError("Patrol deadlines must leave a finite work and cleanup budget")
        work_deadline = deadline - reserve
        totals = None
        remaining_ids = None
        def should_stop():
            return ((cancelled is not None and cancelled()) or
                    (ownership_deadline is not None and time.monotonic() >= ownership_deadline()))
        with self._lock:
            if self._quarantined:
                raise PatrolSupervisorQuarantined("Previous resource generation remains quarantined")
            while time.monotonic() < work_deadline:
                self._last_progress, self._selected_ids = None, None
                self._finished_ids, self._timed_out_item = set(), None
                self._batch_requires_retire = False
                try:
                    if self._process is not None and (
                        self._tasks >= self._max_tasks or time.monotonic() - self._started_at >= self._max_runtime
                    ):
                        self._retire(min(deadline, time.monotonic() + reserve))
                    if self._process is None:
                        self._start(work_deadline, should_stop)
                    request_id = uuid.uuid4().hex
                    progress_path = Path(self._profile_directory) / "progress.json"
                    progress_path.unlink(missing_ok=True)
                    _write_atomic_message(Path(self._profile_directory) / "request.json", {
                        "kind": "batch", "request_id": request_id, "limit": limit,
                        "deadline": work_deadline, "item_timeout": max(1, item_timeout - reserve),
                        "product_ids": remaining_ids, "scheduler_ownership": scheduler_ownership,
                        "soft_sold_counts": self._soft_sold_counts,
                    })
                    summary = self._wait_batch(request_id, work_deadline, cancelled, ownership_deadline)
                    if self._batch_requires_retire:
                        self._retire(min(deadline, time.monotonic() + reserve))
                    self._tasks += summary.get("attempted_count", 0)
                    if totals is None:
                        return summary
                    return _merge_batch_summary(totals, summary)
                except OperationDeadlineExceeded:
                    self._soft_sold_counts = {}
                    progress = self._last_progress
                    self._retire(min(deadline, time.monotonic() + reserve))
                    if progress is None:
                        raise
                    partial = dict(progress["summary"])
                    if self._timed_out_item is not None:
                        partial["error_count"] += 1
                        site = progress.get("active_site")
                        counts = partial["site_results"].setdefault(site, {
                            "successful_count": 0, "error_count": 0, "inconclusive_count": 0, "reason": None,
                        })
                        counts["error_count"] += 1
                        counts["reason"] = "operation_timeout"
                    partial["status"] = "deadline_exceeded"
                    partial["unprocessed_count"] = partial["selected_count"] - sum(
                        partial.get(key, 0) for key in (
                            "successful_count", "error_count", "discarded_count", "inconclusive_count"))
                    totals = partial if totals is None else _merge_batch_summary(totals, partial)
                    original_ids = self._selected_ids or []
                    remaining_ids = [product_id for product_id in original_ids if product_id not in self._finished_ids
                                     and product_id != self._timed_out_item]
                    if not remaining_ids or deadline - time.monotonic() <= reserve + 25:
                        return totals
                    limit = len(remaining_ids)
                except BaseException:
                    self._soft_sold_counts = {}
                    self._retire(min(deadline, time.monotonic() + reserve))
                    raise
            if totals is not None:
                return totals
            raise OperationDeadlineExceeded("No patrol batch budget remains")


def _merge_batch_summary(previous, current):
    merged = dict(previous)
    for key in ("successful_count", "error_count", "updated_count", "discarded_count",
                "inconclusive_count", "attempted_count", "paused_count", "resumed_count"):
        merged[key] = previous.get(key, 0) + current.get(key, 0)
    # A continuation observes the original selected set, never claims that
    # interrupted items succeeded. Changed/deleted remaining rows are discarded.
    missing = max(0, previous.get("unprocessed_count", 0) - current.get("selected_count", 0))
    merged["discarded_count"] += missing
    merged["unprocessed_count"] = merged["selected_count"] - sum(
        merged.get(key, 0) for key in ("successful_count", "error_count", "discarded_count", "inconclusive_count"))
    merged["status"] = current["status"]
    merged["site_results"] = {site: dict(counts) for site, counts in previous.get("site_results", {}).items()}
    for site, counts in current.get("site_results", {}).items():
        target = merged["site_results"].setdefault(site, {
            "successful_count": 0, "error_count": 0, "inconclusive_count": 0, "reason": None,
        })
        for key in ("successful_count", "error_count", "inconclusive_count"):
            target[key] += counts.get(key, 0)
        if target["reason"] is None and counts.get("reason"):
            target["reason"] = counts["reason"]
    if merged["unprocessed_count"] and merged["status"] in {"completed", "no_products"}:
        merged["status"] = "deadline_exceeded"
    elif merged["selected_count"] and merged["status"] == "no_products":
        # The continuation may find only deleted/changed remaining rows; the
        # original batch still selected and accounted for its earlier items.
        merged["status"] = "completed"
    return merged


_singleton = None
_runner_singleton = None
_singleton_lock = threading.Lock()


def get_patrol_supervisor():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PatrolSupervisor()
            atexit.register(_close_supervisor)
        return _singleton


def get_supervised_patrol_runner():
    global _runner_singleton
    with _singleton_lock:
        if _runner_singleton is None:
            _runner_singleton = SupervisedPatrolRunner()
            atexit.register(_close_supervisor)
        return _runner_singleton


def _close_supervisor():
    if _singleton is not None or _runner_singleton is not None:
        try:
            close_patrol_supervisor()
        except Exception:
            logger.exception("Patrol fetch resources remain quarantined at exit")


def close_patrol_supervisor():
    """Public worker/scheduler shutdown hook; does not initialize a child."""
    if _singleton is not None:
        _singleton.close()
    if _runner_singleton is not None:
        _runner_singleton.close()
