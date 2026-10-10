"""Hard-bounded scheduler heartbeat writes, including Redis DNS and cleanup.

The child entrypoint imports only the standard library and redis. It never
loads Flask, the application, SQLAlchemy, a browser or an arbitrary callable.
Secrets are read from a private request file, never from argv or logs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit


OWNER_WRITE_LUA = (
    "if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end "
    "for i=2,#ARGV,2 do redis.call('HSET',KEYS[2],ARGV[i],ARGV[i+1]) end "
    "return 1"
)
_MAX_REQUEST_BYTES = 128 * 1024
_MAX_RESULT_BYTES = 4096
_POLL_SECONDS = 0.05
_admission_lock = threading.Lock()


@dataclass
class _OwnedChild:
    process: subprocess.Popen
    known: dict = field(default_factory=dict)


_quarantined_children: list[_OwnedChild] = []


def _result(status: str, error_type: str | None = None) -> dict[str, str]:
    payload = {"status": status}
    if error_type:
        payload["error_type"] = (
            error_type if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,100}", error_type)
            else "HeartbeatTransportError"
        )
    return payload


def _validate_request(request: dict) -> dict:
    if not isinstance(request, dict) or set(request) - {
        "redis_url", "heartbeat_key", "mapping", "lock_key", "owner_token_hex",
    }:
        raise ValueError("Invalid heartbeat request fields")
    url = request.get("redis_url")
    if not isinstance(url, str) or urlsplit(url).scheme not in {"redis", "rediss", "unix"}:
        raise ValueError("Invalid heartbeat Redis transport")
    key = request.get("heartbeat_key")
    mapping = request.get("mapping")
    if not isinstance(key, str) or not key or len(key) > 1024:
        raise ValueError("Invalid heartbeat key")
    if not isinstance(mapping, dict) or not mapping or len(mapping) > 256:
        raise ValueError("Invalid heartbeat mapping")
    for name, value in mapping.items():
        if not isinstance(name, str) or not name or len(name) > 256 or not isinstance(value, str):
            raise ValueError("Heartbeat mapping must contain serialized strings")
    if bool(request.get("lock_key")) != bool(request.get("owner_token_hex")):
        raise ValueError("Heartbeat owner key and token must be paired")
    if request.get("lock_key"):
        if not isinstance(request["lock_key"], str) or len(request["lock_key"]) > 1024:
            raise ValueError("Invalid heartbeat owner key")
        token = request["owner_token_hex"]
        if not isinstance(token, str) or not token or len(token) > 512 or len(token) % 2 or not re.fullmatch(r"[A-Fa-f0-9]+", token):
            raise ValueError("Invalid heartbeat owner token")
    return request


def write_request(request: dict, *, redis_factory=None) -> dict[str, str]:
    """Child-only Redis operation; owner verification and HSET are atomic."""
    try:
        request = _validate_request(request)
        if redis_factory is None:
            from redis import Redis

            redis_factory = Redis.from_url
        connection = redis_factory(
            request["redis_url"], socket_connect_timeout=2, socket_timeout=2,
            retry_on_timeout=False,
        )
        try:
            if request.get("lock_key"):
                arguments: list[Any] = [bytes.fromhex(request["owner_token_hex"])]
                for name, value in request["mapping"].items():
                    arguments.extend((name, value))
                written = connection.eval(
                    OWNER_WRITE_LUA, 2, request["lock_key"], request["heartbeat_key"], *arguments,
                )
                return _result("written" if written else "ownership_lost")
            connection.hset(request["heartbeat_key"], mapping=request["mapping"])
            return _result("written")
        finally:
            connection.close()
    except Exception as exc:
        # Never serialize Redis exceptions: their messages can contain URLs,
        # passwords, server details or arbitrary heartbeat values.
        return _result("failed", type(exc).__name__)


def _observe(child: _OwnedChild) -> None:
    from services.patrol_supervisor import observe_owned_process

    observe_owned_process(child.process, known=child.known, session=child.process.pid)


def _stop(child: _OwnedChild, *, deadline: float, allowance: float) -> bool:
    try:
        from services.patrol_supervisor import terminate_owned_process

        terminate_owned_process(
            child.process, deadline=deadline, session=child.process.pid, known=child.known,
            cleanup_seconds=max(0, allowance / 3), kill_seconds=max(0, allowance * 2 / 3),
        )
        return True
    except Exception:
        return False


def _write_private_json(path: Path, payload: dict) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(serialized) > _MAX_REQUEST_BYTES:
        raise ValueError("Heartbeat request exceeded size limit")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(serialized)


def bounded_write(
    request: dict,
    *,
    timeout_seconds: float = 5.0,
    cancelled=None,
) -> dict[str, str]:
    """No network I/O occurs in this parent, and cleanup fits inside the budget.

    An unverified child remains quarantined across later calls. Failed or
    timed-out writes never fall back to an in-process Redis connection.
    """
    global _quarantined_children
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        return _result("failed", "ValueError")
    if timeout_seconds == 0:
        return _result("timeout", "HeartbeatAdmissionTimeout")
    deadline = time.monotonic() + timeout_seconds
    try:
        _validate_request(request)
        while True:
            if cancelled is not None and cancelled():
                return _result("cancelled", "HeartbeatCancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _result("timeout", "HeartbeatAdmissionTimeout")
            if _admission_lock.acquire(timeout=min(_POLL_SECONDS, remaining)):
                break
    except Exception as exc:
        return _result("failed", type(exc).__name__)
    try:
        survivors = []
        for child in _quarantined_children:
            if not _stop(child, deadline=deadline, allowance=min(0.5, max(0, deadline - time.monotonic()))):
                survivors.append(child)
        _quarantined_children = survivors
        if survivors:
            return _result("quarantined", "HeartbeatCleanupIncomplete")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _result("timeout", "HeartbeatAdmissionTimeout")
        cleanup_reserve = min(2.0, remaining / 3)
        with tempfile.TemporaryDirectory(prefix="esp-heartbeat-") as directory:
            request_file, result_file = Path(directory) / "request.json", Path(directory) / "result.json"
            _write_private_json(request_file, request)
            process = subprocess.Popen(
                [sys.executable, "-m", "services.heartbeat_transport", "--child",
                 str(request_file), str(result_file), str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parent.parent), start_new_session=True,
            )
            child = _OwnedChild(process)
            outcome = None
            try:
                work_deadline = deadline - cleanup_reserve
                while True:
                    _observe(child)
                    if cancelled is not None and cancelled():
                        outcome = _result("cancelled", "HeartbeatCancelled")
                        break
                    if process.poll() is not None:
                        break
                    remaining = work_deadline - time.monotonic()
                    if remaining <= 0:
                        outcome = _result("timeout", "HeartbeatTimeout")
                        break
                    try:
                        process.wait(timeout=min(_POLL_SECONDS, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            except Exception as exc:
                outcome = _result("failed", type(exc).__name__)
            except BaseException:
                if not _stop(child, deadline=deadline, allowance=cleanup_reserve):
                    _quarantined_children.append(child)
                raise
            if not _stop(child, deadline=deadline, allowance=cleanup_reserve):
                _quarantined_children.append(child)
                return _result("quarantined", "HeartbeatCleanupIncomplete")
            if outcome is not None:
                return outcome
            if process.returncode != 0 or not result_file.exists() or result_file.stat().st_size > _MAX_RESULT_BYTES:
                return _result("failed", "HeartbeatChildFailed")
            result = json.loads(result_file.read_text())
            if not isinstance(result, dict) or result.get("status") not in {"written", "ownership_lost", "failed"}:
                return _result("failed", "HeartbeatProtocolError")
            return _result(result["status"], result.get("error_type"))
    except Exception as exc:
        return _result("failed", type(exc).__name__)
    finally:
        _admission_lock.release()


def _watch_parent(parent_pid: int) -> None:
    while os.getppid() == parent_pid:
        time.sleep(0.1)
    os.killpg(os.getpid(), signal.SIGKILL)


def _child(request_file: str, result_file: str, parent_pid: int) -> int:
    threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True).start()
    try:
        path = Path(request_file)
        if path.stat().st_size > _MAX_REQUEST_BYTES or path.stat().st_mode & 0o077:
            raise ValueError("Heartbeat request file is not private")
        result = write_request(json.loads(path.read_text()))
    except Exception as exc:
        result = _result("failed", type(exc).__name__)
    _write_private_json(Path(result_file), result)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--child":
        raise SystemExit(2)
    raise SystemExit(_child(sys.argv[2], sys.argv[3], int(sys.argv[4])))
