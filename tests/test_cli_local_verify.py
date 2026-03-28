import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_local_verify_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_local_verification_suite",
        lambda current_app, **kwargs: {
            "ready": True,
            "profile": kwargs["profile"],
            "steps": [{"name": "predeploy-single-web", "ready": True, "blockers": []}],
            "blockers": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["local-verify", "--profile", "parser"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["profile"] == "parser"


def test_local_verify_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_local_verification_suite",
        lambda current_app, **kwargs: {
            "ready": False,
            "profile": kwargs["profile"],
            "steps": [{"name": "db-smoke", "ready": False, "blockers": ["redis_connection_failed"]}],
            "blockers": ["db-smoke"],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["local-verify", "--profile", "stack"])

    assert result.exit_code == 1


def test_run_local_verification_suite_parser_profile(monkeypatch, app):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "ready": True,
            "blockers": [],
        },
    )

    detail_steps = []

    def fake_detail(site, fixture_path, *, target_url="", strict=False):
        detail_steps.append((site, fixture_path, target_url, strict))
        return {
            "ready": True,
            "site": site,
            "status": "on_sale" if "live" in fixture_path or site == "snkrdunk" else "deleted",
            "page_type": "active_detail" if "live" in fixture_path or site == "snkrdunk" else "deleted_detail",
            "warnings": [],
            "blockers": [],
        }

    monkeypatch.setattr("cli.run_detail_fixture_smoke", fake_detail)
    monkeypatch.setattr(
        "cli.run_search_fixture_smoke",
        lambda site, fixture_path, *, target_url="", strict=False: {
            "ready": False,
            "site": site,
            "page_type": "search_skeleton",
            "item_count": 0,
            "warnings": [],
            "blockers": ["search_results_not_rendered"],
        },
    )
    monkeypatch.setattr(
        "cli.run_single_web_smoke",
        lambda current_app, **kwargs: {
            "ready": True,
            "queue_backend": "inmemory",
            "mode": kwargs["mode"],
            "blockers": [],
        },
    )

    from cli import run_local_verification_suite

    snapshot = run_local_verification_suite(app, profile="parser", strict_parser=True)

    assert snapshot["ready"] is True
    assert [step["name"] for step in snapshot["steps"]] == [
        "predeploy-single-web",
        "single-web-smoke-preview",
        "detail-mercari-active",
        "detail-mercari-deleted",
        "search-mercari-fixture",
        "detail-snkrdunk-active",
    ]
    assert snapshot["steps"][0]["advisory"] is True
    assert snapshot["steps"][1]["advisory"] is False
    assert snapshot["steps"][4]["advisory"] is True
    assert detail_steps[0][0] == "mercari"
    assert detail_steps[0][3] is True
    assert detail_steps[1][0] == "mercari"
    assert detail_steps[1][3] is False
    assert detail_steps[2][0] == "snkrdunk"


def test_run_local_verification_suite_stack_profile(monkeypatch, app):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "ready": True,
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_database_smoke_check",
        lambda **kwargs: {"ready": True, "blockers": [], "database_backend": kwargs["require_backend"]},
    )

    stack_calls = []

    def fake_stack(current_app, **kwargs):
        stack_calls.append(kwargs)
        return {
            "ready": True,
            "queue_name": "stack-smoke-test",
            "job_id": f"{kwargs['fixture_site']}-job",
            "mode": kwargs["mode"],
            "blockers": [],
        }

    monkeypatch.setattr("cli.run_stack_smoke", fake_stack)

    from cli import run_local_verification_suite

    snapshot = run_local_verification_suite(app, profile="stack", require_backend="postgresql")

    assert snapshot["ready"] is True
    assert [step["name"] for step in snapshot["steps"]] == [
        "predeploy-single-web",
        "predeploy-split-render",
        "db-smoke",
        "stack-mercari-persist",
        "stack-snkrdunk-persist",
    ]
    assert snapshot["steps"][0]["advisory"] is True
    assert snapshot["steps"][1]["advisory"] is True
    assert [call["fixture_site"] for call in stack_calls] == ["mercari", "snkrdunk"]


def test_run_local_verification_suite_full_profile_includes_fixture_backed_single_web_persist(monkeypatch, app):
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "ready": True,
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_detail_fixture_smoke",
        lambda site, fixture_path, *, target_url="", strict=False: {
            "ready": True,
            "site": site,
            "status": "on_sale",
            "page_type": "active_detail",
            "warnings": [],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_search_fixture_smoke",
        lambda site, fixture_path, *, target_url="", strict=False: {
            "ready": False,
            "site": site,
            "page_type": "search_skeleton",
            "item_count": 0,
            "warnings": [],
            "blockers": ["search_results_not_rendered"],
        },
    )
    monkeypatch.setattr(
        "cli.run_database_smoke_check",
        lambda **kwargs: {"ready": True, "blockers": [], "database_backend": kwargs["require_backend"]},
    )

    single_web_calls = []

    def fake_single_web(current_app, **kwargs):
        single_web_calls.append(kwargs)
        return {
            "ready": True,
            "queue_backend": "inmemory",
            "mode": kwargs["mode"],
            "blockers": [],
        }

    monkeypatch.setattr("cli.run_single_web_smoke", fake_single_web)

    stack_calls = []

    def fake_stack(current_app, **kwargs):
        stack_calls.append(kwargs)
        return {
            "ready": True,
            "queue_name": "stack-smoke-test",
            "job_id": f"{kwargs['fixture_site']}-job",
            "mode": kwargs["mode"],
            "blockers": [],
        }

    monkeypatch.setattr("cli.run_stack_smoke", fake_stack)

    from cli import run_local_verification_suite

    snapshot = run_local_verification_suite(app, profile="full", require_backend="postgresql")

    assert snapshot["ready"] is True
    assert [step["name"] for step in snapshot["steps"]] == [
        "predeploy-single-web",
        "predeploy-split-render",
        "single-web-smoke-preview",
        "single-web-smoke-mercari-persist",
        "single-web-smoke-snkrdunk-persist",
        "detail-mercari-active",
        "detail-mercari-deleted",
        "search-mercari-fixture",
        "detail-snkrdunk-active",
        "db-smoke",
        "stack-mercari-persist",
        "stack-snkrdunk-persist",
    ]
    assert [call["mode"] for call in single_web_calls] == ["preview", "persist", "persist"]
    assert single_web_calls[1]["fixture_site"] == "mercari"
    assert single_web_calls[2]["fixture_site"] == "snkrdunk"
    assert [call["fixture_site"] for call in stack_calls] == ["mercari", "snkrdunk"]


def test_run_render_cutover_readiness_aggregates_split_readiness(monkeypatch, app):
    split_web_app = object()
    split_worker_app = object()

    monkeypatch.setattr("app.create_web_app", lambda **kwargs: split_web_app)
    monkeypatch.setattr("app.create_worker_app", lambda **kwargs: split_worker_app)

    def fake_predeploy(current_app, target="single-web"):
        if current_app is app and target == "single-web":
            return {"target": target, "ready": True, "blockers": [], "warnings": []}
        if current_app is split_web_app and target == "split-render":
            return {"target": target, "ready": True, "blockers": [], "warnings": []}
        raise AssertionError(f"unexpected predeploy target={target!r}")

    monkeypatch.setattr("cli.build_predeploy_snapshot", fake_predeploy)
    monkeypatch.setattr(
        "cli.get_worker_health_snapshot",
        lambda current_app: {
            "queue_backend": "rq",
            "redis_ok": True,
            "redis_error": None,
            "backlog_issues": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_render_blueprint_audit",
        lambda path="render.yaml": {
            "ready": True,
            "blockers": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_local_verification_suite",
        lambda current_app, **kwargs: {
            "ready": True,
            "profile": "full",
            "steps": [{"name": "stack-mercari-persist", "ready": True, "blockers": []}],
            "blockers": [],
        },
    )

    from cli import run_render_cutover_readiness

    snapshot = run_render_cutover_readiness(app, strict=True)

    assert snapshot["ready"] is True
    assert snapshot["expected_render_services"] == [
        "esp-web",
        "esp-worker",
        "esp-keyvalue",
        "esp-postgres",
    ]
    assert [step["name"] for step in snapshot["steps"]] == [
        "current-single-web-predeploy",
        "split-render-predeploy",
        "render-blueprint-audit",
        "split-render-worker-health",
        "local-verify-full",
    ]
    assert snapshot["steps"][0]["advisory"] is True
    assert snapshot["steps"][1]["advisory"] is False
    assert snapshot["steps"][4]["profile"] == "full"


def test_run_render_cutover_readiness_strict_mode_elevates_split_warnings(monkeypatch, app):
    split_web_app = object()
    split_worker_app = object()

    monkeypatch.setattr("app.create_web_app", lambda **kwargs: split_web_app)
    monkeypatch.setattr("app.create_worker_app", lambda **kwargs: split_worker_app)
    monkeypatch.setattr(
        "cli.build_predeploy_snapshot",
        lambda current_app, target="single-web": {
            "target": target,
            "ready": True,
            "blockers": [],
            "warnings": ["web_scheduler_mode_is_unexpected"] if target == "split-render" else [],
        },
    )
    monkeypatch.setattr(
        "cli.get_worker_health_snapshot",
        lambda current_app: {
            "queue_backend": "rq",
            "redis_ok": True,
            "redis_error": None,
            "backlog_issues": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_render_blueprint_audit",
        lambda path="render.yaml": {
            "ready": True,
            "blockers": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        "cli.run_local_verification_suite",
        lambda current_app, **kwargs: {
            "ready": True,
            "profile": "full",
            "steps": [],
            "blockers": [],
        },
    )

    from cli import run_render_cutover_readiness

    snapshot = run_render_cutover_readiness(app, strict=True)

    assert snapshot["ready"] is False
    assert snapshot["blockers"] == ["split-render-predeploy"]
