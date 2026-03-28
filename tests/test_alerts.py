from services.alerts import AlertDispatcher


def test_notify_operational_issue_uses_operational_webhook(monkeypatch):
    sent = []
    dispatcher = AlertDispatcher(sender=lambda url, payload: sent.append((url, payload)))

    monkeypatch.setenv("OPERATIONAL_ALERT_WEBHOOK_URL", "https://alerts.example.test/ops")

    delivered = dispatcher.notify_operational_issue(
        event_type="worker_backlog_warning",
        component="worker_runtime",
        severity="warning",
        message="Backlog exceeded threshold",
        details={"queued_count": 99},
    )

    assert delivered is True
    assert sent[0][0] == "https://alerts.example.test/ops"
    assert sent[0][1]["category"] == "operational"
    assert sent[0][1]["event_type"] == "worker_backlog_warning"
    assert sent[0][1]["component"] == "worker_runtime"
    assert sent[0][1]["details"]["queued_count"] == 99


def test_notify_operational_issue_respects_cooldown(monkeypatch):
    sent = []
    dispatcher = AlertDispatcher(sender=lambda url, payload: sent.append((url, payload)))

    monkeypatch.setenv("OPERATIONAL_ALERT_WEBHOOK_URL", "https://alerts.example.test/ops")
    monkeypatch.setenv("OPERATIONAL_ALERT_COOLDOWN_SECONDS", "3600")

    first = dispatcher.notify_operational_issue(
        event_type="worker_backlog_warning",
        component="worker_runtime",
        message="first",
    )
    second = dispatcher.notify_operational_issue(
        event_type="worker_backlog_warning",
        component="worker_runtime",
        message="second",
    )

    assert first is True
    assert second is False
    assert len(sent) == 1
