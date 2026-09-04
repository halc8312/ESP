import logging
import os
import subprocess

import pytest

from services import recordcity_virtual_display as virtual_display


class _FakeProcess:
    def __init__(self, *, timeout_once=False, stdout=None):
        self.stdout = stdout
        self.timeout_once = timeout_once
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1
        if self.timeout_once and self.waited == 1:
            raise subprocess.TimeoutExpired("Xvfb", timeout)
        return 0


@pytest.fixture(autouse=True)
def clear_recordcity_display_env(monkeypatch):
    monkeypatch.delenv("RECORDCITY_BROWSER_PROFILE", raising=False)
    monkeypatch.delenv("RECORDCITY_FETCH_PROVIDER", raising=False)


def test_xvfb_command_allocates_display_without_network_listener():
    command = virtual_display._xvfb_command()

    assert command[:3] == ["Xvfb", "-displayfd", "1"]
    assert command[command.index("-nolisten") + 1] == "tcp"
    assert not any("vnc" in value.lower() or "debug" in value.lower() for value in command)


def test_headless_profile_does_not_start_xvfb(monkeypatch):
    monkeypatch.setenv("RECORDCITY_BROWSER_PROFILE", "headless")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        virtual_display,
        "_start_xvfb",
        lambda: pytest.fail("Xvfb must not start for headless Record City"),
    )

    with virtual_display.recordcity_virtual_display():
        assert "DISPLAY" not in os.environ


@pytest.mark.parametrize(
    "profile",
    ["persistent-chrome", "patchright-persistent-chrome"],
)
def test_persistent_chrome_profile_requests_private_display(monkeypatch, profile):
    monkeypatch.setenv("RECORDCITY_BROWSER_PROFILE", profile)
    monkeypatch.delenv("DISPLAY", raising=False)
    process = _FakeProcess()
    stopped = []
    monkeypatch.setattr(virtual_display, "_start_xvfb", lambda: (process, ":78"))
    monkeypatch.setattr(virtual_display, "_stop_xvfb", stopped.append)

    with virtual_display.recordcity_virtual_display():
        assert os.environ["DISPLAY"] == ":78"

    assert "DISPLAY" not in os.environ
    assert stopped == [process]


def test_external_provider_does_not_start_unused_xvfb(monkeypatch):
    monkeypatch.setenv("RECORDCITY_BROWSER_PROFILE", "headful")
    monkeypatch.setenv("RECORDCITY_FETCH_PROVIDER", "zyte")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        virtual_display,
        "_start_xvfb",
        lambda: pytest.fail("external provider must not start Xvfb"),
    )

    with virtual_display.recordcity_virtual_display():
        assert "DISPLAY" not in os.environ


def test_existing_display_is_respected(monkeypatch):
    monkeypatch.setenv("RECORDCITY_BROWSER_PROFILE", "headful")
    monkeypatch.setenv("DISPLAY", ":44")
    monkeypatch.setattr(
        virtual_display,
        "_start_xvfb",
        lambda: pytest.fail("existing DISPLAY must be reused"),
    )

    with virtual_display.recordcity_virtual_display():
        assert os.environ["DISPLAY"] == ":44"

    assert os.environ["DISPLAY"] == ":44"


def test_managed_display_is_restored_and_stopped_after_worker_error(monkeypatch):
    monkeypatch.setenv("RECORDCITY_BROWSER_PROFILE", "headful")
    monkeypatch.delenv("DISPLAY", raising=False)
    process = _FakeProcess()
    stopped = []
    monkeypatch.setattr(virtual_display, "_start_xvfb", lambda: (process, ":77"))
    monkeypatch.setattr(virtual_display, "_stop_xvfb", stopped.append)

    with pytest.raises(RuntimeError, match="worker failed"):
        with virtual_display.recordcity_virtual_display():
            assert os.environ["DISPLAY"] == ":77"
            raise RuntimeError("worker failed")

    assert "DISPLAY" not in os.environ
    assert stopped == [process]


def test_start_xvfb_uses_private_process_and_allocated_display(monkeypatch):
    captured = {}
    process = _FakeProcess()

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(virtual_display.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(virtual_display, "_read_display_number", lambda value: ":81")

    result_process, display = virtual_display._start_xvfb()

    assert result_process is process
    assert display == ":81"
    assert captured["command"] == virtual_display._xvfb_command()
    assert captured["kwargs"]["start_new_session"] is True
    assert "shell" not in captured["kwargs"]


def test_read_display_number_accepts_only_numeric_display():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"83\n")
    os.close(write_fd)
    stdout = os.fdopen(read_fd)
    process = _FakeProcess(stdout=stdout)
    try:
        assert virtual_display._read_display_number(process) == ":83"
    finally:
        stdout.close()


def test_read_display_number_rejects_untrusted_output():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"not-a-display\n")
    os.close(write_fd)
    stdout = os.fdopen(read_fd)
    process = _FakeProcess(stdout=stdout)
    try:
        with pytest.raises(RuntimeError, match="RC_XVFB_START_FAILED"):
            virtual_display._read_display_number(process)
    finally:
        stdout.close()


def test_start_xvfb_reports_executable_failure_without_command_text(monkeypatch):
    monkeypatch.setattr(
        virtual_display.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("secret")),
    )

    with pytest.raises(RuntimeError) as exc_info:
        virtual_display._start_xvfb()

    assert "RC_XVFB_START_FAILED" in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_start_xvfb_stops_process_when_readiness_fails(monkeypatch):
    process = _FakeProcess()
    stopped = []
    monkeypatch.setattr(virtual_display.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        virtual_display,
        "_read_display_number",
        lambda _process: (_ for _ in ()).throw(
            RuntimeError("reason=RC_XVFB_START_TIMEOUT")
        ),
    )
    monkeypatch.setattr(virtual_display, "_stop_xvfb", stopped.append)

    with pytest.raises(RuntimeError, match="RC_XVFB_START_TIMEOUT"):
        virtual_display._start_xvfb()

    assert stopped == [process]


def test_cleanup_failure_does_not_mask_worker_error(monkeypatch, caplog):
    monkeypatch.setenv("RECORDCITY_BROWSER_PROFILE", "headful")
    monkeypatch.delenv("DISPLAY", raising=False)

    class _BrokenProcess(_FakeProcess):
        def poll(self):
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(
        virtual_display,
        "_start_xvfb",
        lambda: (_BrokenProcess(), ":82"),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="worker failed"):
            with virtual_display.recordcity_virtual_display():
                raise ValueError("worker failed")

    assert caplog.text.count("Record City virtual display cleanup failed") == 1


def test_xvfb_stop_escalates_to_kill_after_timeout():
    process = _FakeProcess(timeout_once=True)

    virtual_display._stop_xvfb(process)

    assert process.terminated == 1
    assert process.killed == 1
    assert process.waited == 2
