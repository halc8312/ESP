import json


def test_worker_health_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.get_worker_health_snapshot",
        lambda current_app: {
            "queue_backend": "rq",
            "redis_ok": True,
            "backlog_issues": [],
            "queue_name": "worker-q",
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["worker-health"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["queue_backend"] == "rq"
    assert payload["redis_ok"] is True


def test_worker_health_cli_fails_when_redis_is_unhealthy(app, monkeypatch):
    monkeypatch.setattr(
        "cli.get_worker_health_snapshot",
        lambda current_app: {
            "queue_backend": "rq",
            "redis_ok": False,
            "backlog_issues": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["worker-health"])

    assert result.exit_code == 1


def test_worker_health_cli_fail_on_warning_respects_backlog_issues(app, monkeypatch):
    monkeypatch.setattr(
        "cli.get_worker_health_snapshot",
        lambda current_app: {
            "queue_backend": "rq",
            "redis_ok": True,
            "backlog_issues": ["queued_count>=25"],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["worker-health", "--fail-on-warning"])

    assert result.exit_code == 1
