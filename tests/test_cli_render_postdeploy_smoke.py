import json


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def _ready_payload():
    return {
        "status": "ready",
        "checks": {
            "database": "ok",
            "redis": "ok",
            "worker": "ok",
            "scheduler": "ok",
            "patrol": "ok",
        },
        "runtime_role": "web",
        "queue_backend": "rq",
        "scheduler_enabled": False,
    }


def _csrf_form(token="test-csrf-token"):
    return f'<form><input name="csrf_token" value="{token}"></form>'


def test_run_render_postdeploy_smoke_success(monkeypatch):
    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        if url.endswith("/login"):
            return _FakeResponse(200)
        if url.endswith("/scrape"):
            return _FakeResponse(302, headers={"Location": "/login"})
        if url.endswith("/api/scrape/jobs"):
            return _FakeResponse(302, headers={"Location": "/login"})
        raise AssertionError(url)

    monkeypatch.setattr("cli.requests.get", fake_get)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com")

    assert snapshot["ready"] is True
    assert snapshot["blockers"] == []
    assert snapshot["health_payload"]["queue_backend"] == "rq"
    assert snapshot["readiness_url"] == "https://example.com/stack-readyz"
    assert len(snapshot["route_checks"]) == 3
    assert snapshot["health_attempt_count"] == 1
    assert snapshot["retry_policy"]["retries"] == 2


def test_run_render_postdeploy_smoke_requires_dependency_readiness(monkeypatch):
    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(
                503,
                {
                    "status": "not_ready",
                    "checks": {"database": "unavailable"},
                },
            )
        if url.endswith("/healthz"):
            return _FakeResponse(200, {"status": "ok", "runtime_role": "web"})
        if url.endswith("/login"):
            return _FakeResponse(200)
        return _FakeResponse(302, headers={"Location": "/login"})

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.time.sleep", lambda _: None)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke(
        "https://example.com",
        retries=0,
    )

    assert snapshot["ready"] is False
    assert snapshot["readiness_status_code"] == 503
    assert "stack_readyz_status_not_200" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_blocks_stale_worker_and_scheduler(monkeypatch):
    readiness = _ready_payload()
    readiness.update(
        {
            "status": "not_ready",
            "checks": {
                "database": "ok",
                "redis": "ok",
                "worker": "stale",
                "scheduler": "unavailable",
            },
        }
    )

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(503, readiness)
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        if url.endswith("/login"):
            return _FakeResponse(200)
        return _FakeResponse(302, headers={"Location": "/login"})

    monkeypatch.setattr("cli.requests.get", fake_get)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke(
        "https://example.com",
        retries=0,
    )

    assert snapshot["ready"] is False
    assert "stack_readyz_worker_not_ready" in snapshot["blockers"]
    assert "stack_readyz_scheduler_not_ready" in snapshot["blockers"]
    assert "stack_readyz_patrol_not_ready" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_rejects_empty_ready_payload(monkeypatch):
    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, {})
        if url.endswith("/healthz"):
            return _FakeResponse(200, {"status": "ok", "runtime_role": "web"})
        if url.endswith("/login"):
            return _FakeResponse(200)
        return _FakeResponse(302, headers={"Location": "/login"})

    monkeypatch.setattr("cli.requests.get", fake_get)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com")

    assert snapshot["ready"] is False
    assert "stack_readyz_status_not_ready" in snapshot["blockers"]
    assert "queue_backend_not_exposed" in snapshot["blockers"]
    assert "scheduler_mode_not_exposed" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_rejects_empty_health_payload(monkeypatch):
    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(200, {})
        if url.endswith("/login"):
            return _FakeResponse(200)
        return _FakeResponse(302, headers={"Location": "/login"})

    monkeypatch.setattr("cli.requests.get", fake_get)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com")

    assert snapshot["ready"] is False
    assert "healthz_status_not_ok" in snapshot["blockers"]
    assert "runtime_role_mismatch" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_rejects_runtime_contract_mismatch(monkeypatch):
    readiness = _ready_payload()
    readiness.update({"queue_backend": "inmemory", "scheduler_enabled": True})

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, readiness)
        if url.endswith("/healthz"):
            return _FakeResponse(200, {"status": "ok", "runtime_role": "web"})
        if url.endswith("/login"):
            return _FakeResponse(200)
        return _FakeResponse(302, headers={"Location": "/login"})

    monkeypatch.setattr("cli.requests.get", fake_get)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com")

    assert snapshot["ready"] is False
    assert "queue_backend_mismatch" in snapshot["blockers"]
    assert "scheduler_mode_mismatch" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_retries_readyz(monkeypatch):
    calls = {"readyz": 0}

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            calls["readyz"] += 1
            if calls["readyz"] == 1:
                return _FakeResponse(503, {"status": "not_ready"})
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(200, {"status": "ok", "runtime_role": "web"})
        if url.endswith("/login"):
            return _FakeResponse(200)
        return _FakeResponse(302, headers={"Location": "/login"})

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.time.sleep", lambda _: None)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke(
        "https://example.com",
        retries=2,
        retry_delay_seconds=0,
    )

    assert snapshot["ready"] is True
    assert snapshot["readiness_attempt_count"] == 2
    assert "stack_readyz_required_retry:2" in snapshot["warnings"]


def test_run_render_postdeploy_smoke_retries_healthz_until_ready(monkeypatch):
    calls = {"healthz": 0}

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            calls["healthz"] += 1
            if calls["healthz"] == 1:
                return _FakeResponse(503)
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        if url.endswith("/login"):
            return _FakeResponse(200)
        if url.endswith("/scrape"):
            return _FakeResponse(302, headers={"Location": "/login"})
        if url.endswith("/api/scrape/jobs"):
            return _FakeResponse(302, headers={"Location": "/login"})
        raise AssertionError(url)

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.time.sleep", lambda _: None)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com", retries=2, retry_delay_seconds=0)

    assert snapshot["ready"] is True
    assert snapshot["health_attempt_count"] == 2
    assert "healthz_required_retry:2" in snapshot["warnings"]


def test_run_render_postdeploy_smoke_with_authenticated_routes(monkeypatch):
    class FakeSession:
        def post(self, url, data=None, timeout=0, allow_redirects=True):
            assert url.endswith("/login")
            assert data == {
                "username": "smoke",
                "password": "secret",
                "csrf_token": "test-csrf-token",
            }
            return _FakeResponse(302, headers={"Location": "/"})

        def get(self, url, timeout=0, allow_redirects=True):
            if url.endswith("/login"):
                return _FakeResponse(200, text=_csrf_form())
            if url.endswith("/scrape"):
                return _FakeResponse(200)
            if url.endswith("/api/scrape/jobs"):
                return _FakeResponse(200)
            raise AssertionError(url)

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        if url.endswith("/login"):
            return _FakeResponse(200)
        if url.endswith("/scrape"):
            return _FakeResponse(302, headers={"Location": "/login"})
        if url.endswith("/api/scrape/jobs"):
            return _FakeResponse(302, headers={"Location": "/login"})
        raise AssertionError(url)

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.requests.Session", lambda: FakeSession())

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com", username="smoke", password="secret")

    assert snapshot["ready"] is True
    assert snapshot["login_success"] is True
    assert len(snapshot["authenticated_route_checks"]) == 2


def test_run_render_postdeploy_smoke_can_register_smoke_user_when_login_fails(monkeypatch):
    class FakeSession:
        def post(self, url, data=None, timeout=0, allow_redirects=True):
            if url.endswith("/login"):
                return _FakeResponse(200)
            if url.endswith("/register"):
                assert data == {
                    "username": "smoke",
                    "password": "secret",
                    "csrf_token": "register-csrf-token",
                }
                return _FakeResponse(302, headers={"Location": "/"})
            raise AssertionError(url)

        def get(self, url, timeout=0, allow_redirects=True):
            if url.endswith("/login"):
                return _FakeResponse(200, text=_csrf_form())
            if url.endswith("/register"):
                return _FakeResponse(
                    200,
                    text=_csrf_form("register-csrf-token"),
                )
            if url.endswith("/scrape"):
                return _FakeResponse(200)
            if url.endswith("/api/scrape/jobs"):
                return _FakeResponse(200)
            raise AssertionError(url)

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        if url.endswith("/login"):
            return _FakeResponse(200)
        if url.endswith("/scrape"):
            return _FakeResponse(302, headers={"Location": "/login"})
        if url.endswith("/api/scrape/jobs"):
            return _FakeResponse(302, headers={"Location": "/login"})
        raise AssertionError(url)

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.requests.Session", lambda: FakeSession())

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke(
        "https://example.com",
        username="smoke",
        password="secret",
        ensure_user=True,
    )

    assert snapshot["ready"] is True
    assert snapshot["login_success"] is False
    assert snapshot["registration_attempted"] is True
    assert snapshot["registration_success"] is True
    assert len(snapshot["authenticated_route_checks"]) == 2


def test_run_render_postdeploy_smoke_flags_server_error_route(monkeypatch):
    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        if url.endswith("/api/scrape/jobs"):
            return _FakeResponse(500)
        return _FakeResponse(200)

    monkeypatch.setattr("cli.requests.get", fake_get)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com")

    assert snapshot["ready"] is False
    assert "route_server_error:/api/scrape/jobs" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_flags_authenticated_jobs_route_server_error(monkeypatch):
    class FakeSession:
        def post(self, url, data=None, timeout=0, allow_redirects=True):
            return _FakeResponse(302, headers={"Location": "/"})

        def get(self, url, timeout=0, allow_redirects=True):
            if url.endswith("/login"):
                return _FakeResponse(200, text=_csrf_form())
            if url.endswith("/scrape"):
                return _FakeResponse(200)
            if url.endswith("/api/scrape/jobs"):
                return _FakeResponse(500)
            raise AssertionError(url)

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        return _FakeResponse(200)

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.requests.Session", lambda: FakeSession())

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com", username="smoke", password="secret")

    assert snapshot["ready"] is False
    assert "authenticated_route_server_error:/api/scrape/jobs" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_requires_complete_auth_credentials(monkeypatch):
    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        return _FakeResponse(200)

    monkeypatch.setattr("cli.requests.get", fake_get)

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com", username="smoke")

    assert snapshot["ready"] is False
    assert "auth_credentials_incomplete" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_requires_authenticated_session_when_auth_requested(monkeypatch):
    class FakeSession:
        def post(self, url, data=None, timeout=0, allow_redirects=True):
            return _FakeResponse(200)

        def get(self, url, timeout=0, allow_redirects=True):
            if url.endswith("/login"):
                return _FakeResponse(200, text=_csrf_form())
            raise AssertionError(url)

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "runtime_role": "web",
                    "queue_backend": "rq",
                    "scheduler_enabled": False,
                },
            )
        return _FakeResponse(200)

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.requests.Session", lambda: FakeSession())

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke("https://example.com", username="smoke", password="secret")

    assert snapshot["ready"] is False
    assert "authenticated_session_not_established" in snapshot["blockers"]


def test_run_render_postdeploy_smoke_requires_login_csrf_token(monkeypatch):
    class FakeSession:
        def get(self, url, timeout=0, allow_redirects=True):
            if url.endswith("/login"):
                return _FakeResponse(200, text="<form></form>")
            raise AssertionError(url)

        def post(self, url, data=None, timeout=0, allow_redirects=True):
            raise AssertionError("POST must not occur without a CSRF token")

    def fake_get(url, timeout=0, allow_redirects=True):
        if url.endswith("/stack-readyz"):
            return _FakeResponse(200, _ready_payload())
        if url.endswith("/healthz"):
            return _FakeResponse(200, {"status": "ok", "runtime_role": "web"})
        if url.endswith("/login"):
            return _FakeResponse(200)
        return _FakeResponse(302, headers={"Location": "/login"})

    monkeypatch.setattr("cli.requests.get", fake_get)
    monkeypatch.setattr("cli.requests.Session", lambda: FakeSession())

    from cli import run_render_postdeploy_smoke

    snapshot = run_render_postdeploy_smoke(
        "https://example.com",
        username="smoke",
        password="secret",
    )

    assert snapshot["ready"] is False
    assert "login_csrf_token_missing" in snapshot["blockers"]
    assert "authenticated_session_not_established" in snapshot["blockers"]


def test_render_postdeploy_smoke_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_postdeploy_smoke",
        lambda base_url, **kwargs: {
            "ready": True,
            "blockers": [],
            "base_url": base_url,
            "retry_policy": {"retries": kwargs.get("retries"), "retry_delay_seconds": kwargs.get("retry_delay_seconds")},
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-postdeploy-smoke", "--base-url", "https://example.com"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["retry_policy"]["retries"] == 2


def test_render_postdeploy_smoke_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_postdeploy_smoke",
        lambda base_url, **kwargs: {
            "ready": False,
            "blockers": ["healthz_request_failed"],
            "base_url": base_url,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-postdeploy-smoke", "--base-url", "https://example.com"])

    assert result.exit_code == 1
