import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.alerts import AlertDispatcher


@pytest.fixture(autouse=True)
def isolate_alert_config(monkeypatch):
    for category in ("SELECTOR", "OPERATIONAL", "SCRAPE"):
        for setting in ("WEBHOOK_URL", "COOLDOWN_SECONDS", "MAX_PER_WINDOW", "WINDOW_SECONDS"):
            monkeypatch.delenv(f"{category}_ALERT_{setting}", raising=False)


def scrape_result(dispatcher, *, event_type="site_unhealthy", **kwargs):
    return dispatcher.notify_scrape_issue_result(
        event_type=event_type, site="mercari", page_type="detail", **kwargs
    )


def test_unconfigured_is_distinct_from_failed_and_does_not_call_sender():
    sent = []
    dispatcher = AlertDispatcher(sender=lambda *args: sent.append(args))

    assert dispatcher.scrape_webhook_configured is False
    assert dispatcher.operational_webhook_configured is False
    assert scrape_result(dispatcher) == {"status": "unconfigured", "error_type": None}
    assert dispatcher.notify_scrape_issue(
        event_type="site_unhealthy", site="mercari", page_type="detail"
    ) is False
    assert sent == []


@pytest.mark.parametrize("category", ["SCRAPE", "SELECTOR", "OPERATIONAL"])
def test_configured_boolean_includes_fallbacks(monkeypatch, category):
    monkeypatch.setenv(f"{category}_ALERT_WEBHOOK_URL", " https://alerts.example.test/private-token ")
    dispatcher = AlertDispatcher()

    assert dispatcher.scrape_webhook_configured is True
    assert dispatcher.operational_webhook_configured is (category == "OPERATIONAL")


def test_failed_send_does_not_consume_cooldown_or_rate_budget(monkeypatch):
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", "https://alerts.example.test/private-token")
    monkeypatch.setenv("SCRAPE_ALERT_MAX_PER_WINDOW", "1")
    attempts = []

    def sender(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise TimeoutError("do not log this request")

    dispatcher = AlertDispatcher(sender=sender)

    assert scrape_result(dispatcher) == {"status": "failed", "error_type": "TimeoutError"}
    assert dispatcher._last_sent_by_key == {}
    assert dispatcher._global_sent_at == []
    assert scrape_result(dispatcher) == {"status": "delivered", "error_type": None}
    assert scrape_result(dispatcher)["status"] == "cooldown"
    assert scrape_result(dispatcher, event_type="different_event")["status"] == "rate_limited"
    assert len(attempts) == 2


def test_cooldown_starts_at_acceptance_not_at_start(monkeypatch):
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", "https://alerts.example.test/scrape")
    monkeypatch.setenv("SCRAPE_ALERT_COOLDOWN_SECONDS", "10")
    now = [100.0]
    monkeypatch.setattr("services.alerts.time.monotonic", lambda: now[0])

    def sender(*_args):
        now[0] += 20

    dispatcher = AlertDispatcher(sender=sender)
    assert scrape_result(dispatcher)["status"] == "delivered"
    assert scrape_result(dispatcher)["status"] == "cooldown"
    now[0] += 10
    assert scrape_result(dispatcher)["status"] == "delivered"


def test_successful_global_budget_expires(monkeypatch):
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", "https://alerts.example.test/scrape")
    monkeypatch.setenv("SCRAPE_ALERT_MAX_PER_WINDOW", "1")
    monkeypatch.setenv("SCRAPE_ALERT_WINDOW_SECONDS", "10")
    now = [100.0]
    monkeypatch.setattr("services.alerts.time.monotonic", lambda: now[0])
    dispatcher = AlertDispatcher(sender=lambda *_args: None)

    assert scrape_result(dispatcher)["status"] == "delivered"
    now[0] += 5
    assert scrape_result(dispatcher, event_type="next_event")["status"] == "rate_limited"
    now[0] += 6
    assert scrape_result(dispatcher, event_type="next_event")["status"] == "delivered"


def test_short_window_does_not_erase_shared_long_window_budget(monkeypatch):
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", "https://alerts.example.test/scrape")
    monkeypatch.setenv("OPERATIONAL_ALERT_WEBHOOK_URL", "https://alerts.example.test/ops")
    monkeypatch.setenv("SCRAPE_ALERT_WINDOW_SECONDS", "10")
    monkeypatch.setenv("OPERATIONAL_ALERT_WINDOW_SECONDS", "300")
    monkeypatch.setenv("OPERATIONAL_ALERT_MAX_PER_WINDOW", "2")
    now = [100.0]
    monkeypatch.setattr("services.alerts.time.monotonic", lambda: now[0])
    dispatcher = AlertDispatcher(sender=lambda *_args: None)

    assert dispatcher.notify_operational_issue_result(
        event_type="worker_unhealthy", component="worker"
    )["status"] == "delivered"
    now[0] += 20
    assert scrape_result(dispatcher)["status"] == "delivered"
    assert dispatcher.notify_operational_issue_result(
        event_type="scheduler_unhealthy", component="scheduler"
    )["status"] == "rate_limited"


def test_concurrent_duplicate_is_in_flight_not_a_second_send(monkeypatch):
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", "https://alerts.example.test/scrape")
    monkeypatch.setenv("SCRAPE_ALERT_COOLDOWN_SECONDS", "0")
    started = threading.Event()
    release = threading.Event()
    attempts = []

    def sender(*args):
        attempts.append(args)
        started.set()
        assert release.wait(5)

    dispatcher = AlertDispatcher(sender=sender)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(scrape_result, dispatcher)
        try:
            assert started.wait(5)
            assert scrape_result(dispatcher) == {"status": "in_flight", "error_type": None}
            assert len(attempts) == 1
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "delivered"
    assert dispatcher._in_flight_keys == set()


def test_in_flight_reserves_global_capacity_and_failure_releases_it(monkeypatch):
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", "https://alerts.example.test/scrape")
    monkeypatch.setenv("SCRAPE_ALERT_MAX_PER_WINDOW", "1")
    started = threading.Event()
    release = threading.Event()
    attempts = []

    def sender(*args):
        attempts.append(args)
        if len(attempts) == 1:
            started.set()
            assert release.wait(5)
            raise ConnectionError("a private request body")

    dispatcher = AlertDispatcher(sender=sender)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(scrape_result, dispatcher)
        try:
            assert started.wait(5)
            assert scrape_result(dispatcher, event_type="second_event")["status"] == "rate_limited"
        finally:
            release.set()
        assert first.result(timeout=5) == {"status": "failed", "error_type": "ConnectionError"}
    assert scrape_result(dispatcher, event_type="second_event")["status"] == "delivered"
    assert len(attempts) == 2


def test_base_exception_releases_in_flight_reservation(monkeypatch):
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", "https://alerts.example.test/scrape")

    def sender(*_args):
        raise KeyboardInterrupt()

    dispatcher = AlertDispatcher(sender=sender)
    with pytest.raises(KeyboardInterrupt):
        scrape_result(dispatcher)

    assert dispatcher._in_flight_keys == set()
    assert dispatcher._last_sent_by_key == {}
    assert dispatcher._global_sent_at == []


def test_failure_logs_do_not_include_secrets_or_payload(monkeypatch, caplog):
    webhook = "https://alerts.example.test/private-webhook-token"
    monkeypatch.setenv("SCRAPE_ALERT_WEBHOOK_URL", webhook)

    def sender(*_args):
        raise RuntimeError(f"{webhook} payload=secret-body")

    dispatcher = AlertDispatcher(sender=sender)
    with caplog.at_level(logging.DEBUG, logger="alerts"):
        result = scrape_result(dispatcher, message="secret-body", dedupe_key="private-dedupe-token")

    assert result == {"status": "failed", "error_type": "RuntimeError"}
    assert "category=scrape event=site_unhealthy status=failed error_type=RuntimeError" in caplog.text
    for secret in (webhook, "private-webhook-token", "secret-body", "private-dedupe-token"):
        assert secret not in caplog.text


def test_invalid_event_is_not_emitted_into_structured_log(monkeypatch, caplog):
    dispatcher = AlertDispatcher()
    with caplog.at_level(logging.DEBUG, logger="alerts"):
        scrape_result(dispatcher, event_type="https://private.example.test/token\nfake=event")

    assert "event=unknown" in caplog.text
    assert "private.example" not in caplog.text
    assert "fake=event" not in caplog.text


def test_operational_result_api_and_bool_wrapper_are_compatible(monkeypatch):
    monkeypatch.setenv("OPERATIONAL_ALERT_WEBHOOK_URL", "https://alerts.example.test/ops")
    dispatcher = AlertDispatcher(sender=lambda *_args: None)

    assert dispatcher.notify_operational_issue_result(
        event_type="worker_unhealthy", component="worker"
    ) == {"status": "delivered", "error_type": None}
    assert dispatcher.notify_operational_issue(
        event_type="worker_unhealthy", component="worker"
    ) is False
    assert dispatcher.notify_operational_issue(
        event_type="scheduler_unhealthy", component="scheduler"
    ) is True


def test_selector_boolean_api_retries_after_failure(monkeypatch):
    monkeypatch.setenv("SELECTOR_ALERT_WEBHOOK_URL", "https://alerts.example.test/selector")
    attempts = []

    def sender(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise TimeoutError()

    dispatcher = AlertDispatcher(sender=sender)
    kwargs = dict(event_type="repair_candidate", site="mercari", page_type="detail", field="title")
    assert dispatcher.notify_selector_issue(**kwargs) is False
    assert dispatcher.notify_selector_issue(**kwargs) is True
    assert dispatcher.notify_selector_issue(**kwargs) is False
