import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_render_worker_postdeploy_checklist_reads_worker_contract_from_blueprint():
    from cli import run_render_worker_postdeploy_checklist

    snapshot = run_render_worker_postdeploy_checklist("render.yaml")

    assert snapshot["ready"] is True
    assert snapshot["service_name"] == "esp-worker"
    assert snapshot["expected_runtime"]["docker_command"] == "python worker.py"
    assert snapshot["expected_runtime"]["queue_backend"] == "rq"
    assert snapshot["expected_runtime"]["scheduler_enabled"] is True
    assert snapshot["expected_runtime"]["warm_browser_pool"] is True
    assert snapshot["expected_runtime"]["browser_pool_warm_sites"] == ["mercari"]
    assert snapshot["expected_runtime"]["process_selector_repairs_on_startup"] is False
    assert snapshot["expected_runtime"]["selector_repair_limit"] == 1
    assert snapshot["expected_runtime"]["selector_repair_min_score"] == 90
    assert snapshot["expected_runtime"]["selector_repair_min_canaries"] == 2
    assert "Worker browser pool warmed:" in snapshot["expected_log_markers"]
    assert "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL" in snapshot["manual_envs"]
    assert any("process-selector-repairs --limit 1 --dry-run" in check for check in snapshot["manual_checks"])


def test_render_worker_postdeploy_checklist_cli_prints_json(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-worker-postdeploy-checklist", "--blueprint-path", "render.yaml"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
    assert payload["service_name"] == "esp-worker"
