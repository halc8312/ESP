import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_render_local_split_checklist_reports_local_contract(app, monkeypatch):
    from cli import run_render_local_split_checklist

    monkeypatch.setattr(
        "cli._probe_local_split_services",
        lambda database_url, redis_url: {
            "ready": True,
            "blockers": [],
            "probes": [
                {"name": "postgres", "url": database_url, "ready": True, "blockers": []},
                {"name": "redis", "url": redis_url, "ready": True, "blockers": []},
            ],
        },
    )

    snapshot = run_render_local_split_checklist(app, blueprint_path="render.yaml", compose_path="docker-compose.local.yml")

    assert snapshot["compose_file_present"] is True
    assert snapshot["rehearsal_commands"][0] == "docker compose -f docker-compose.local.yml up -d"
    assert snapshot["local_env_contract"][0]["key"] == "SECRET_KEY"
    assert snapshot["local_env_contract"][1]["key"] == "DATABASE_URL"
    assert snapshot["local_env_contract"][3]["key"] == "SCRAPE_QUEUE_BACKEND"
    assert snapshot["powershell_env_commands"][0].startswith("$env:SECRET_KEY=")
    assert snapshot["budget_guardrail"]["estimated_monthly_core_usd"] == 61
    assert snapshot["service_probes"]["ready"] is True
    assert snapshot["service_probes"]["probes"][0]["name"] == "postgres"


def test_render_local_split_checklist_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli._probe_local_split_services",
        lambda database_url, redis_url: {
            "ready": True,
            "blockers": [],
            "probes": [
                {"name": "postgres", "url": database_url, "ready": True, "blockers": []},
                {"name": "redis", "url": redis_url, "ready": True, "blockers": []},
            ],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-local-split-checklist", "--blueprint-path", "render.yaml"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["compose_file_present"] is True
    assert payload["runbook_path"] == "docs/RENDER_CUTOVER_RUNBOOK.md"
    assert payload["service_probes"]["ready"] is True
