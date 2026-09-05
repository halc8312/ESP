import io
import json
from email.message import Message
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError

import pytest

from scripts import esp_health_watchdog as watchdog


URL = "https://esp-1-kend.onrender.com"


def ready_payload():
    return {
        "status": "ready",
        "checks": {name: "ok" for name in watchdog.REQUIRED_CHECKS},
        "runtime_role": "web",
        "queue_backend": "rq",
        "scheduler_enabled": False,
    }


class FakeResponse(io.BytesIO):
    def __init__(self, payload, *, status=200, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        super().__init__(raw)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_healthy_requires_all_checks_and_makes_one_bounded_request():
    response = FakeResponse(ready_payload())
    opener = FakeOpener(response)
    result = watchdog.check_stack(URL, opener=opener)
    assert result["status"] == "healthy"
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.full_url == URL + "/stack-readyz"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") is None
    assert timeout == 10
    assert response.closed


@pytest.mark.parametrize("state", ["stale", "failed", "degraded", "unavailable", "no_observations"])
def test_non_ok_patrol_cannot_be_green_even_if_http_200(state):
    payload = ready_payload()
    payload["checks"]["patrol"] = state
    result = watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(payload)))
    assert result["status"] != "healthy"
    assert result["failed_checks"] == ["patrol"]
    if state == "no_observations":
        assert result["status"] == "unverified"


def test_503_body_is_evaluated_without_retry():
    payload = ready_payload()
    payload["status"] = "not_ready"
    payload["checks"]["worker"] = "stale"
    headers = Message()
    headers["Content-Type"] = "application/json"
    error = HTTPError(URL, 503, "secret response message", headers, io.BytesIO(json.dumps(payload).encode()))
    opener = FakeOpener(error)
    result = watchdog.check_stack(URL, opener=opener)
    assert result["code"] == "stack_unhealthy"
    assert result["failed_checks"] == ["worker"]
    assert len(opener.calls) == 1
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("url", [
    "http://esp-1-kend.onrender.com",
    "https://example.com",
    "https://unapproved.onrender.com",
    "https://esp-1-kend.onrender.com.evil.test",
    "https://127.0.0.1",
    "https://[::1]",
    "https://localhost",
    "https://user:secret@esp-1-kend.onrender.com",
    "https://esp-1-kend.onrender.com:444",
    "https://esp-1-kend.onrender.com/readyz",
    "https://esp-1-kend.onrender.com?token=secret",
    "https://esp-1-kend.onrender.com/#secret",
    "https://esp-1-kend.onrender.com\n",
])
def test_rejects_unapproved_target_without_request_or_secret_output(url):
    opener = FakeOpener(None)
    result = watchdog.check_stack(url, opener=opener)
    assert result == {"status": "error", "code": "invalid_configuration"}
    assert opener.calls == []


@pytest.mark.parametrize("hosts", [("*.onrender.com",), ("localhost",), ("127.0.0.1",), ()])
def test_allowlist_cannot_authorize_private_or_arbitrary_hosts(hosts):
    opener = FakeOpener(None)
    assert watchdog.check_stack(URL, allowed_hosts=hosts, opener=opener)["code"] == "invalid_configuration"
    assert opener.calls == []


def test_verified_alternate_render_service_can_be_configured():
    result = watchdog.check_stack(
        "https://esp-web-new.onrender.com/stack-readyz",
        allowed_hosts=("esp-web-new.onrender.com",),
        opener=FakeOpener(FakeResponse(ready_payload())),
    )
    assert result["status"] == "healthy"


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 429, 500])
def test_other_http_statuses_do_not_retry_or_read_body(status):
    opener = FakeOpener(FakeResponse(b"secret body", status=status))
    result = watchdog.check_stack(URL, opener=opener)
    assert result == {"status": "error", "code": "unexpected_http_status", "http_status": status}
    assert len(opener.calls) == 1


def test_redirect_handler_never_follows_redirects():
    assert watchdog._NoRedirect().redirect_request(None, None, 302, "", {}, URL) is None


def test_default_opener_disables_environment_proxies_and_redirects(monkeypatch):
    captured = {}

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener(FakeResponse(ready_payload()))

    monkeypatch.setenv("HTTPS_PROXY", "https://user:secret@untrusted.invalid")
    monkeypatch.setattr(watchdog, "build_opener", fake_build_opener)
    assert watchdog.check_stack(URL)["status"] == "healthy"
    proxy, redirect = captured["handlers"]
    assert proxy.proxies == {}
    assert isinstance(redirect, watchdog._NoRedirect)


@pytest.mark.parametrize("status, declared", [(503, "ready"), (200, "not_ready")])
def test_inconsistent_all_ok_response_is_not_healthy(status, declared):
    payload = ready_payload()
    payload["status"] = declared
    assert watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(payload, status=status)))["code"] == "inconsistent_response"


@pytest.mark.parametrize("mutate", [
    lambda p: p["checks"].update({"worker": "secret unknown value"}),
    lambda p: p.update({"runtime_role": "worker"}),
    lambda p: p.update({"queue_backend": "inmemory"}),
    lambda p: p.update({"scheduler_enabled": "false"}),
    lambda p: p.update({"checks": None}),
])
def test_missing_or_wrong_schema_is_not_healthy(mutate):
    payload = ready_payload()
    mutate(payload)
    result = watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(payload)))
    assert result == {"status": "error", "code": "invalid_response"}


@pytest.mark.parametrize("missing", watchdog.REQUIRED_CHECKS)
def test_missing_evidence_including_older_monitor_deployment_is_unverified(missing):
    payload = ready_payload()
    payload["checks"].pop(missing)
    result = watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(payload)))
    assert result == {"status": "unverified", "code": "incomplete_checks", "missing_checks": [missing]}


@pytest.mark.parametrize("state", ["stale", "failed", "unavailable"])
def test_live_scheduler_cannot_hide_stopped_or_failing_scrape_monitor(state):
    payload = ready_payload()
    payload["checks"]["scrape_monitor"] = state
    result = watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(payload)))
    assert result["code"] == "stack_unhealthy"
    assert result["failed_checks"] == ["scrape_monitor"]
    assert result["checks"]["scheduler"] == "ok"


@pytest.mark.parametrize("payload", [[], None, b"not json", b"\xff"])
def test_bad_json_shape_or_encoding_is_sanitized(payload):
    assert watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(payload)))["code"] == "invalid_response"


def test_oversized_and_non_json_response_are_rejected():
    assert watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(b"x" * (watchdog.MAX_RESPONSE_BYTES + 1))))["code"] == "oversized_response"
    assert watchdog.check_stack(URL, opener=FakeOpener(FakeResponse(b"secret", content_type="text/html")))["code"] == "invalid_response"


@pytest.mark.parametrize("error", [URLError("https://user:secret@host"), TimeoutError("secret"), OSError("secret"), IncompleteRead(b"secret")])
def test_network_failure_is_sanitized_and_not_retried(error):
    opener = FakeOpener(error)
    assert watchdog.check_stack(URL, opener=opener) == {"status": "error", "code": "network_failure"}
    assert len(opener.calls) == 1


def test_response_close_exception_does_not_expose_remote_data():
    response = FakeResponse(ready_payload())
    response.close = lambda: (_ for _ in ()).throw(OSError("secret"))
    assert watchdog.check_stack(URL, opener=FakeOpener(response))["status"] == "healthy"


def test_deeply_nested_response_is_safely_rejected():
    response = FakeResponse(b"[" * 2000 + b"0" + b"]" * 2000)
    assert watchdog.check_stack(URL, opener=FakeOpener(response))["code"] == "invalid_response"


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan")])
def test_bad_timeout_never_makes_request(timeout):
    opener = FakeOpener(None)
    assert watchdog.check_stack(URL, timeout=timeout, opener=opener)["code"] == "invalid_configuration"
    assert opener.calls == []


def test_main_unconfigured_is_nonzero_and_not_healthy(monkeypatch, capsys):
    monkeypatch.delenv("ESP_MONITOR_BASE_URL", raising=False)
    assert watchdog.main([]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unconfigured"


def test_main_healthy_returns_zero_without_exposing_config(monkeypatch, capsys):
    monkeypatch.setenv("ESP_MONITOR_BASE_URL", URL)
    monkeypatch.setattr(watchdog, "check_stack", lambda *args, **kwargs: {"status": "healthy", "code": "stack_ready"})
    assert watchdog.main([]) == 0
    assert URL not in capsys.readouterr().out


def test_main_unverified_returns_failure_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(watchdog, "check_stack", lambda *args, **kwargs: {"status": "unverified", "code": "no_observations"})
    assert watchdog.main(["--base-url", URL]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "no_observations"
