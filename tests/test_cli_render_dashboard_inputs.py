import json

import pytest


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_render_dashboard_inputs_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_dashboard_inputs",
        lambda blueprint_path="render.yaml": {
            "ready": True,
            "blockers": [],
            "warnings": [],
            "service_inputs": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-dashboard-inputs"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True


def test_render_dashboard_inputs_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_dashboard_inputs",
        lambda blueprint_path="render.yaml": {
            "ready": False,
            "blockers": ["missing_service:esp-web"],
            "warnings": [],
            "service_inputs": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-dashboard-inputs"])

    assert result.exit_code == 1


@pytest.mark.parametrize(
    "blueprint_path",
    ["render.yaml", "render.existing-web-addons.yaml"],
)
def test_render_dashboard_inputs_exposes_pinned_recordcity_worker_env(blueprint_path):
    from cli import run_render_dashboard_inputs

    snapshot = run_render_dashboard_inputs(blueprint_path)
    worker = next(
        service for service in snapshot["service_inputs"] if service["service"] == "esp-worker"
    )
    fixed_envs = {env["key"]: env["value"] for env in worker["fixed_envs"]}
    warm_sites = {
        site.strip().lower()
        for site in fixed_envs["BROWSER_POOL_WARM_SITES"].split(",")
        if site.strip()
    }

    assert fixed_envs["RECORDCITY_BROWSER_PROFILE"] == "headful"
    assert fixed_envs["RECORDCITY_FETCH_PROVIDER"] == "browser"
    assert not any(site.startswith("recordcity") for site in warm_sites)
