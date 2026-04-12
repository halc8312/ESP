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


def test_prepare_outbound_payload_for_discord_webhook_uses_discord_fields():
    payload = {
        "text": "[selector-healer][warning] repair_candidate_recorded mercari/detail/title",
        "category": "selector",
        "event_type": "repair_candidate_recorded",
        "severity": "warning",
        "site": "mercari",
        "page_type": "detail",
        "field": "title",
        "message": "Recorded a candidate after a successful heal.",
        "details": {"candidate_id": 321, "score": 97},
        "dedupe_key": "selector:repair_candidate_recorded:mercari:detail:title",
        "timestamp": "2026-04-12T09:00:00+00:00",
    }

    outbound = AlertDispatcher._prepare_outbound_payload(
        "https://discord.com/api/webhooks/123/abc",
        payload,
    )

    assert outbound["content"] == payload["text"]
    assert outbound["username"] == "ESP Alerts"
    assert outbound["allowed_mentions"] == {"parse": []}
    assert outbound["embeds"][0]["title"] == payload["text"][:256]
    assert outbound["embeds"][0]["description"] == payload["message"]
    assert outbound["embeds"][0]["timestamp"] == payload["timestamp"]
    detail_field = next(field for field in outbound["embeds"][0]["fields"] if field["name"] == "Details")
    assert "\"candidate_id\": 321" in detail_field["value"]
    assert "\"score\": 97" in detail_field["value"]


def test_prepare_outbound_payload_preserves_generic_webhooks():
    payload = {"text": "plain payload", "details": {"foo": "bar"}}

    outbound = AlertDispatcher._prepare_outbound_payload(
        "https://alerts.example.test/hooks/selector",
        payload,
    )

    assert outbound == payload


def test_build_request_headers_sets_explicit_user_agent():
    headers = AlertDispatcher._build_request_headers()

    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["User-Agent"].startswith("ESP-Alerts/")
