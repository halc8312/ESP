import json

from cli import run_single_web_smoke


def test_single_web_smoke_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_single_web_smoke",
        lambda current_app, **kwargs: {
            "queue_backend": "inmemory",
            "blockers": [],
            "ready": True,
            "mode": kwargs["mode"],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["single-web-smoke", "--mode", "preview"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["queue_backend"] == "inmemory"
    assert payload["ready"] is True
    assert payload["mode"] == "preview"


def test_single_web_smoke_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_single_web_smoke",
        lambda current_app, **kwargs: {
            "queue_backend": "inmemory",
            "blockers": ["single_web_smoke_job_not_completed"],
            "ready": False,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["single-web-smoke"])

    assert result.exit_code == 1


def test_run_single_web_smoke_preview_mode(app):
    snapshot = run_single_web_smoke(app, mode="preview")

    assert snapshot["ready"] is True
    assert snapshot["queue_backend"] == "inmemory"
    assert snapshot["status_api_status_code"] == 200
    assert snapshot["result_page_status_code"] == 200
    assert snapshot["result_page_contains_title"] is True
    assert snapshot["status_payload"]["status"] == "completed"
    assert snapshot["persistence"]["product_count"] == 0


def test_run_single_web_smoke_uses_app_queue_backend_even_if_env_is_rq(app, monkeypatch):
    monkeypatch.setenv("SCRAPE_QUEUE_BACKEND", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    app.config["SCRAPE_QUEUE_BACKEND"] = "inmemory"

    snapshot = run_single_web_smoke(app, mode="preview")

    assert snapshot["ready"] is True
    assert snapshot["queue_backend"] == "inmemory"
    assert snapshot["status_payload"]["status"] == "completed"


def test_run_single_web_smoke_persist_mode_with_fixture(app):
    snapshot = run_single_web_smoke(
        app,
        mode="persist",
        fixture_site="mercari",
        fixture_path="mercari_page_dump_live.html",
        fixture_target_url="https://jp.mercari.com/item/m71383569733",
    )

    assert snapshot["ready"] is True
    assert snapshot["queue_backend"] == "inmemory"
    assert snapshot["status_api_status_code"] == 200
    assert snapshot["result_page_status_code"] == 200
    assert snapshot["result_page_contains_title"] is True
    assert snapshot["status_payload"]["status"] == "completed"
    assert snapshot["persistence"]["product_count"] == 1
    assert snapshot["persistence"]["snapshot_count"] >= 1
    assert snapshot["persistence"]["variant_count"] >= 1


def test_run_single_web_smoke_persist_mode_with_snkrdunk_fixture(app):
    snapshot = run_single_web_smoke(
        app,
        mode="persist",
        fixture_site="snkrdunk",
        fixture_path="dump.html",
        fixture_target_url="https://snkrdunk.com/products/nike-air-max-95-og-big-bubble-neon-yellow-2025-2026",
    )

    assert snapshot["ready"] is True
    assert snapshot["queue_backend"] == "inmemory"
    assert snapshot["status_api_status_code"] == 200
    assert snapshot["result_page_status_code"] == 200
    assert snapshot["result_page_contains_title"] is True
    assert snapshot["status_payload"]["status"] == "completed"
    assert snapshot["persistence"]["product_count"] == 1
    assert snapshot["persistence"]["snapshot_count"] >= 1
    assert snapshot["persistence"]["variant_count"] >= 1
