"""Read-only CLI checks operate on persisted evidence, never live fetches."""
import json
from types import SimpleNamespace

import pytest

from database import create_isolated_session
from models import ScrapeHealthDelivery, ScrapeHealthObservation
from services import scrape_health as health


def _payload(result):
    return json.loads(result.output.strip().splitlines()[-1])


def test_cli_reports_real_empty_database_as_not_fully_verified(app):
    result = app.test_cli_runner().invoke(args=["scrape-health"])

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["status"] == "not_fully_verified"
    assert len(payload["routes"]) == 24
    assert all(row["status"] == "unobserved" for row in payload["routes"])
    assert all(row["last_success_at"] is None for row in payload["routes"])
    assert all(row["scrape_alert_configured"] is False for row in payload["routes"])


def test_cli_strict_mode_fails_when_only_one_route_has_success_evidence(app):
    assert health.record_scrape_observation(
        site="recordcity", route="search", outcome="success", success_count=1
    )

    result = app.test_cli_runner().invoke(args=["scrape-health", "--fail-on-warning"])

    assert result.exit_code == 1
    payload = _payload(result)
    assert payload["status"] == "not_fully_verified"
    rows = {(row["site"], row["route"]): row for row in payload["routes"]}
    assert rows[("recordcity", "search")]["status"] == "healthy"
    assert rows[("recordcity", "detail")]["status"] == "unobserved"
    assert rows[("recordcity", "patrol")]["status"] == "unobserved"


def test_cli_strict_mode_passes_only_when_all_fixture_routes_have_observed_success(app):
    # Synthetic, offline database records, not real marketplace success claims.
    for site in health.SITES:
        for route in health.ROUTES:
            assert health.record_scrape_observation(
                site=site, route=route, outcome="success", success_count=1
            )

    result = app.test_cli_runner().invoke(args=["scrape-health", "--fail-on-warning"])

    assert result.exit_code == 0
    assert _payload(result)["status"] == "healthy"


def test_cli_does_not_dispatch_pending_alerts_or_mutate_observations(app, monkeypatch):
    for _ in range(2):
        assert health.record_scrape_observation(
            site="recordcity", route="search", outcome="failure", reason="captcha"
        )

    def not_a_read_operation():
        raise AssertionError("The health CLI must not run an evaluator or send alerts")

    monkeypatch.setattr(health, "evaluate_scrape_health", not_a_read_operation)
    monkeypatch.setattr(health, "get_alert_dispatcher", lambda: SimpleNamespace(
        scrape_webhook_configured=False,
        notify_scrape_issue_result=not_a_read_operation,
    ))

    result = app.test_cli_runner().invoke(args=["scrape-health", "--fail-on-warning"])

    assert result.exit_code == 1
    rows = _payload(result)["routes"]
    row = next(row for row in rows if row["site"] == "recordcity" and row["route"] == "search")
    assert row["incident_open"] is True
    assert row["latest_delivery_status"] == "pending"
    with create_isolated_session() as session:
        assert session.query(ScrapeHealthObservation).count() == 2
        assert session.query(ScrapeHealthDelivery).one().status == "pending"


def test_cli_store_failure_is_nonzero_in_strict_mode_and_redacts_error_text(app, monkeypatch):
    def unavailable():
        raise RuntimeError("postgresql://private-user:secret-password@internal-host/db")

    monkeypatch.setattr(health, "create_isolated_session", unavailable)
    result = app.test_cli_runner().invoke(args=["scrape-health", "--fail-on-warning"])

    assert result.exit_code == 1
    assert all(row["status"] == "monitoring_unavailable" for row in _payload(result)["routes"])
    assert "secret-password" not in result.output
    assert "internal-host" not in result.output


def test_cli_unexpected_service_exception_reports_type_not_message(app, monkeypatch):
    def unavailable():
        raise ValueError("private-webhook-token")

    monkeypatch.setattr(health, "list_scrape_health", unavailable)
    result = app.test_cli_runner().invoke(args=["scrape-health"])

    assert result.exit_code == 1
    assert _payload(result) == {"status": "unavailable", "error_type": "ValueError"}
    assert "private-webhook-token" not in result.output


@pytest.mark.parametrize("category", ["SCRAPE", "SELECTOR", "OPERATIONAL"])
def test_cli_config_is_boolean_only_even_with_notification_fallback(app, monkeypatch, category):
    secret_url = "https://alerts.example.test/private-token-do-not-expose"
    monkeypatch.setenv(f"{category}_ALERT_WEBHOOK_URL", secret_url)

    result = app.test_cli_runner().invoke(args=["scrape-health"])

    assert result.exit_code == 0
    assert all(row["scrape_alert_configured"] is True for row in _payload(result)["routes"])
    assert "private-token-do-not-expose" not in result.output
    assert "alerts.example.test" not in result.output
