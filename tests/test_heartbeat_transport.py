import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

import pytest

from services import heartbeat_transport as transport


def request(**fields):
    return {"redis_url": "redis://test-user:not-a-real-secret@localhost:6379/0",
        "heartbeat_key": "test:heartbeat", "mapping": {"event": "patrol_completed", "count": "1"}, **fields}


def test_owner_write_uses_fixed_atomic_lua_and_binary_token():
    calls = []

    class Redis:
        def eval(self, *args):
            calls.append(args)
            return 1

        def close(self):
            calls.append("closed")

    payload = request(lock_key="test:owner", owner_token_hex=b"ownerA".hex())
    assert transport.write_request(payload, redis_factory=lambda *_args, **_kwargs: Redis()) == {"status": "written"}
    assert calls[0] == (transport.OWNER_WRITE_LUA, 2, "test:owner", "test:heartbeat",
        b"ownerA", "event", "patrol_completed", "count", "1")
    assert "GET" in calls[0][0] and "HSET" in calls[0][0]
    assert calls[-1] == "closed"


def test_lost_owner_never_falls_back_to_plain_hset():
    class Redis:
        def eval(self, *_args):
            return 0

        def hset(self, *_args, **_kwargs):
            raise AssertionError("Unfenced write must not run")

        def close(self):
            pass

    assert transport.write_request(request(lock_key="owner", owner_token_hex=b"old".hex()),
        redis_factory=lambda *_args, **_kwargs: Redis()) == {"status": "ownership_lost"}


def test_candidate_write_has_no_owner_and_uses_only_candidate_key():
    calls = []

    class Redis:
        def hset(self, key, **kwargs):
            calls.append((key, kwargs))

        def close(self):
            pass

    payload = request(heartbeat_key="test:heartbeat:candidate:host:123")
    assert transport.write_request(payload, redis_factory=lambda *_args, **_kwargs: Redis())["status"] == "written"
    assert calls == [(payload["heartbeat_key"], {"mapping": payload["mapping"]})]


def test_transport_error_exports_class_not_connection_credentials():
    def fail(*_args, **_kwargs):
        raise ConnectionError("redis://test-user:not-a-real-secret@db failed private value")

    result = transport.write_request(request(), redis_factory=fail)
    assert result == {"status": "failed", "error_type": "ConnectionError"}
    assert "not-a-real-secret" not in str(result)


def test_private_request_file_permissions_and_size_limit(tmp_path):
    path = tmp_path / "request.json"
    transport._write_private_json(path, request())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == request()
    with pytest.raises(ValueError, match="size limit"):
        transport._write_private_json(tmp_path / "huge.json", request(mapping={"field": "x" * 200000}))


@pytest.mark.parametrize("fields", [
    {"lock_key": "owner"}, {"owner_token_hex": "1234"},
    {"lock_key": "owner", "owner_token_hex": "not-hex"},
    {"mapping": {"count": 1}}, {"arbitrary_module": "app"},
])
def test_invalid_request_is_rejected_before_starting_process(fields, monkeypatch):
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("invalid request spawned child"))
    assert transport.bounded_write(request(**fields))["status"] == "failed"


def test_child_module_does_not_import_application_or_flask():
    completed = subprocess.run([sys.executable, "-c",
        "import sys; import redis; import services.heartbeat_transport; "
        "assert 'app' not in sys.modules; assert 'flask' not in sys.modules; "
        "assert 'sqlalchemy' not in sys.modules; assert 'playwright' not in sys.modules"],
        check=False, capture_output=True, timeout=5,
        cwd=str(Path(__file__).resolve().parent.parent))
    assert completed.returncode == 0


def test_hung_redis_dns_child_is_bounded_with_injected_process_view(monkeypatch):
    """Real child/cancellation with synthetic ownership view; not /proc proof."""
    import services.patrol_supervisor as supervisor

    processes = []
    real_popen = subprocess.Popen
    child_code = (
        "import sys,time,redis; "
        "from services.heartbeat_transport import _child; "
        "redis.Redis.from_url=lambda *a,**k:time.sleep(30); "
        "raise SystemExit(_child(sys.argv[1],sys.argv[2],int(sys.argv[3])))"
    )

    def hung_dns(args, **kwargs):
        assert "not-a-real-secret" not in str(args)
        assert b"ownerA".hex() not in str(args)
        assert stat.S_IMODE(Path(args[-3]).stat().st_mode) == 0o600
        process = real_popen([sys.executable, "-c", child_code, *args[-3:]], **kwargs)
        processes.append(process)
        return process

    def injected_table():
        if not processes or processes[0].poll() is not None:
            return {}
        child = processes[0]
        return {child.pid: supervisor._ProcessIdentity(child.pid, 1, os.getpid(), child.pid, child.pid, "S")}

    monkeypatch.setattr(transport.subprocess, "Popen", hung_dns)
    monkeypatch.setattr(supervisor, "_process_table", injected_table)
    started = time.monotonic()
    result = transport.bounded_write(request(lock_key="owner", owner_token_hex=b"ownerA".hex()), timeout_seconds=0.9)
    assert result == {"status": "timeout", "error_type": "HeartbeatTimeout"}
    assert time.monotonic() - started < 1.4
    assert processes[0].poll() is not None


def test_unverified_dead_leader_still_blocks_new_transport(monkeypatch):
    child = transport._OwnedChild(type("Process", (), {"pid": 111, "poll": lambda _self: 0})(), {222: "retained descendant"})
    monkeypatch.setattr(transport, "_quarantined_children", [child])
    monkeypatch.setattr(transport, "_stop", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("quarantine admitted child"))
    assert transport.bounded_write(request())["status"] == "quarantined"
    assert transport._quarantined_children == [child]


def test_cancelled_transport_does_not_admit_process(monkeypatch):
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("cancelled writer admitted child"))
    assert transport.bounded_write(request(), cancelled=lambda: True)["status"] == "cancelled"


def test_exhausted_heartbeat_budget_has_timeout_status_without_admission(monkeypatch):
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("expired heartbeat spawned child"))
    assert transport.bounded_write(request(), timeout_seconds=0) == {
        "status": "timeout", "error_type": "HeartbeatAdmissionTimeout"}


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux /proc ownership verification unavailable")
def test_real_linux_child_with_hung_dns_is_stopped(monkeypatch):
    real_popen = subprocess.Popen
    processes = []

    def hung_dns(args, **kwargs):
        code = "import time; time.sleep(30)"
        process = real_popen([sys.executable, "-c", code], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", hung_dns)
    assert transport.bounded_write(request(), timeout_seconds=0.9)["status"] == "timeout"
    assert processes[0].poll() is not None


@pytest.mark.skipif(not os.environ.get("ESP_TEST_REDIS_URL"), reason="Explicit isolated Redis test URL unavailable")
def test_real_redis_atomic_owner_write_and_late_old_owner():
    from redis import Redis

    url = os.environ["ESP_TEST_REDIS_URL"]
    connection = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    namespace = f"esp:test:heartbeat:{uuid.uuid4().hex}"
    owner, heartbeat = namespace + ":owner", namespace + ":value"
    try:
        connection.set(owner, b"ownerA", ex=30)
        payload = request(redis_url=url, lock_key=owner, heartbeat_key=heartbeat,
            owner_token_hex=b"ownerA".hex(), mapping={"event": "first"})
        assert transport.write_request(payload)["status"] == "written"
        connection.set(owner, b"ownerB", ex=30)
        payload["mapping"] = {"event": "late old owner"}
        assert transport.write_request(payload)["status"] == "ownership_lost"
        assert connection.hget(heartbeat, "event") == b"first"
        assert connection.get(owner) == b"ownerB"
    finally:
        connection.delete(owner, heartbeat)
        connection.close()
