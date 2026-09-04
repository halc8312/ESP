import json
from pathlib import Path

import pytest


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_render_blueprint_audit_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_blueprint_audit",
        lambda blueprint_path="render.yaml": {
            "ready": True,
            "blockers": [],
            "warnings": [],
            "blueprint_path": blueprint_path,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-blueprint-audit"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True


def test_render_blueprint_audit_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_blueprint_audit",
        lambda blueprint_path="render.yaml": {
            "ready": False,
            "blockers": ["missing_service:esp-worker"],
            "warnings": [],
            "blueprint_path": blueprint_path,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-blueprint-audit"])

    assert result.exit_code == 1


def test_run_render_blueprint_audit_current_blueprint_is_ready():
    from cli import run_render_blueprint_audit

    snapshot = run_render_blueprint_audit("render.yaml")

    assert snapshot["ready"] is True
    assert snapshot["blockers"] == []
    assert "esp-web" in snapshot["service_names"]
    assert "esp-worker" in snapshot["service_names"]
    assert "esp-keyvalue" in snapshot["service_names"]
    assert "esp-postgres" in snapshot["database_names"]
    assert {"service": "esp-worker", "key": "SELECTOR_ALERT_WEBHOOK_URL", "required": False} in snapshot["manual_secret_envs"]


@pytest.mark.parametrize(
    "blueprint_path",
    ["render.yaml", "render.existing-web-addons.yaml"],
)
def test_render_blueprints_pin_recordcity_worker_runtime(blueprint_path):
    from cli import _parse_render_blueprint

    blueprint = _parse_render_blueprint(blueprint_path)
    worker = next(
        service for service in blueprint["services"] if service["name"] == "esp-worker"
    )
    worker_env = worker["env_vars"]
    warm_sites = {
        site.strip().lower()
        for site in worker_env["BROWSER_POOL_WARM_SITES"]["value"].split(",")
        if site.strip()
    }

    assert worker["dockerCommand"] == "tini -- python worker.py"
    assert worker_env["RECORDCITY_BROWSER_PROFILE"]["value"] == "persistent-chrome"
    assert worker_env["RECORDCITY_FETCH_PROVIDER"]["value"] == "browser"
    assert not any(site.startswith("recordcity") for site in warm_sites)


@pytest.mark.parametrize(
    ("original", "replacement", "expected_blocker"),
    [
        (
            "dockerCommand: tini -- python worker.py",
            "dockerCommand: python worker.py",
            "worker_command_must_use_tini_worker_entrypoint",
        ),
        (
            "- key: RECORDCITY_BROWSER_PROFILE\n        value: persistent-chrome",
            "- key: RECORDCITY_BROWSER_PROFILE\n        value: headless",
            "worker_recordcity_profile_must_be_persistent_chrome",
        ),
        (
            "- key: RECORDCITY_FETCH_PROVIDER\n        value: browser",
            "- key: RECORDCITY_FETCH_PROVIDER\n        value: auto",
            "worker_recordcity_provider_must_be_browser",
        ),
        (
            "- key: BROWSER_POOL_WARM_SITES\n        value: mercari",
            "- key: BROWSER_POOL_WARM_SITES\n        value: mercari,recordcity_headful",
            "worker_recordcity_runtime_must_not_be_prewarmed",
        ),
    ],
)
def test_render_blueprint_audit_blocks_recordcity_runtime_drift(
    tmp_path,
    original,
    replacement,
    expected_blocker,
):
    from cli import run_render_blueprint_audit

    source = Path("render.yaml").read_text(encoding="utf-8")
    assert original in source
    drifted_blueprint = tmp_path / "render.yaml"
    drifted_blueprint.write_text(source.replace(original, replacement, 1), encoding="utf-8")

    snapshot = run_render_blueprint_audit(str(drifted_blueprint))

    assert snapshot["ready"] is False
    assert expected_blocker in snapshot["blockers"]


def test_run_render_dashboard_inputs_current_blueprint_contains_manual_and_managed_envs():
    from cli import run_render_dashboard_inputs

    snapshot = run_render_dashboard_inputs("render.yaml")

    assert snapshot["ready"] is True
    assert snapshot["blockers"] == []
    web_service = next(service for service in snapshot["service_inputs"] if service["service"] == "esp-web")
    worker_service = next(service for service in snapshot["service_inputs"] if service["service"] == "esp-worker")
    assert any(env["key"] == "SECRET_KEY" for env in web_service["manual_envs"])
    assert any(env["key"] == "DATABASE_URL" for env in web_service["managed_envs"])
    assert any(env["key"] == "SCRAPE_QUEUE_BACKEND" for env in worker_service["fixed_envs"])
    assert any(env["key"] == "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL" for env in worker_service["manual_envs"])
    assert any(
        env["key"] == "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP" and env["value"] == "0"
        for env in worker_service["fixed_envs"]
    )


def test_existing_web_addons_requires_manual_existing_web_public_url():
    from cli import _parse_render_blueprint

    blueprint = _parse_render_blueprint("render.existing-web-addons.yaml")
    worker = next(
        service for service in blueprint["services"] if service["name"] == "esp-worker"
    )

    assert "esp-web" not in {
        service["name"] for service in blueprint["services"]
    }
    assert worker["env_vars"]["WEB_PUBLIC_URL"] == {
        "key": "WEB_PUBLIC_URL",
        "sync": "false",
    }
