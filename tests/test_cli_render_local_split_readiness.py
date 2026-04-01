import json
import os

from flask import Flask


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_render_local_split_readiness_applies_repo_local_env(monkeypatch):
    from cli import run_render_local_split_readiness

    observed = {}
    previous_queue_backend = os.environ.get("SCRAPE_QUEUE_BACKEND")

    def fake_create_cli_app(config_overrides=None):
        app = Flask("fake-cli")
        app.config["TESTING"] = False
        return app

    def fake_checklist(current_app, **kwargs):
        observed["database_url"] = os.environ.get("DATABASE_URL")
        return {
            "ready": True,
            "blockers": [],
            "warnings": [],
            "powershell_env_commands": ["$env:SCRAPE_QUEUE_BACKEND='rq'"],
            "rehearsal_commands": ["flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict"],
        }

    def fake_readiness(current_app, **kwargs):
        observed["queue_backend"] = os.environ.get("SCRAPE_QUEUE_BACKEND")
        return {"ready": True, "blockers": []}

    monkeypatch.setattr("app.create_cli_app", fake_create_cli_app)
    monkeypatch.setattr("cli.run_render_local_split_checklist", fake_checklist)
    monkeypatch.setattr("cli.run_render_cutover_readiness", fake_readiness)

    snapshot = run_render_local_split_readiness(Flask(__name__))

    assert snapshot["ready"] is True
    assert observed["database_url"] == "postgresql+psycopg://esp:esp@localhost:5432/esp_local"
    assert observed["queue_backend"] == "rq"
    assert os.environ.get("SCRAPE_QUEUE_BACKEND") == previous_queue_backend


def test_render_local_split_readiness_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_render_local_split_readiness",
        lambda current_app, **kwargs: {
            "ready": True,
            "steps": [{"name": "render-cutover-readiness", "ready": True, "blockers": []}],
            "blockers": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["render-local-split-readiness"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["ready"] is True
