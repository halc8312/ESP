"""Mail CLI checks must not turn a preview or missing config into a real send."""
import json
from types import SimpleNamespace

import pytest

from services import mail_cli
from services.mail_service import ResendMailer


@pytest.fixture(autouse=True)
def clear_mail_environment(monkeypatch):
    for name in ("RESEND_API_KEY", "MAIL_FROM", "MAIL_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def payload(result):
    return json.loads(result.output.strip().splitlines()[-1])


def configure(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("MAIL_FROM", "noreply@jp-items.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_secret_never_echo")


def forbid_send(*args, **kwargs):
    raise AssertionError("An inspection command must not send mail")


def test_mail_commands_are_registered_and_disabled_by_default(app, monkeypatch):
    monkeypatch.setattr(ResendMailer, "send", forbid_send)
    result = app.test_cli_runner().invoke(args=["mail-check"])
    assert result.exit_code == 2
    assert payload(result)["status"] == "disabled"
    assert payload(result)["network_used"] is False
    assert payload(result)["provider_verified"] is False


def test_mail_check_only_validates_local_config_without_leaking_key(app, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(ResendMailer, "send", forbid_send)
    result = app.test_cli_runner().invoke(args=["mail-check"])
    assert result.exit_code == 0
    assert payload(result)["status"] == "ready"
    assert payload(result)["provider_verified"] is False
    assert "re_test_secret_never_echo" not in result.output


def test_mail_test_defaults_to_offline_preview(app, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(ResendMailer, "send", forbid_send)
    result = app.test_cli_runner().invoke(args=[
        "mail-test", "--to", "operator@example.test", "--delivery-key", "esp-test-v1-a",
    ])
    assert result.exit_code == 0
    assert payload(result)["status"] == "dry_run"
    assert payload(result)["network_used"] is False
    assert "operator@example.test" not in result.output
    assert "re_test_secret_never_echo" not in result.output


def test_preview_missing_settings_does_not_claim_readiness(app, monkeypatch):
    monkeypatch.setattr(ResendMailer, "send", forbid_send)
    result = app.test_cli_runner().invoke(args=[
        "mail-test", "--to", "operator@example.test", "--delivery-key", "esp-test-v1-a",
    ])
    assert result.exit_code == 2
    assert payload(result)["configuration"]["status"] == "disabled"
    assert payload(result)["provider_verified"] is False


@pytest.mark.parametrize("args", [
    ["--to", "private-address-invalid", "--delivery-key", "test-key"],
    ["--to", "operator@example.test", "--delivery-key", "bad key\nprivate-value"],
])
def test_invalid_test_input_is_rejected_before_dispatch(app, monkeypatch, args):
    configure(monkeypatch)
    monkeypatch.setattr(ResendMailer, "send", forbid_send)
    result = app.test_cli_runner().invoke(args=["mail-test", *args, "--send"])
    assert result.exit_code == 2
    assert payload(result)["status"] == "invalid_message"
    assert payload(result)["network_used"] is False
    assert "private-" not in result.output


def test_explicit_send_passes_stable_key_and_reports_acceptance_not_receipt(app, monkeypatch):
    configure(monkeypatch)
    sent = []

    def fake_send(self, message, *, idempotency_key):
        sent.append((message, idempotency_key))
        return SimpleNamespace(
            status="accepted", to_dict=lambda: {"status": "accepted", "code": "api_accepted"},
        )

    monkeypatch.setattr(ResendMailer, "send", fake_send)
    result = app.test_cli_runner().invoke(args=[
        "mail-test", "--to", "operator@example.test", "--delivery-key", "esp-test-v1-a", "--send",
    ])
    assert result.exit_code == 0
    assert len(sent) == 1
    message, key = sent[0]
    assert message.to == "operator@example.test"
    assert message.subject == mail_cli.TEST_SUBJECT
    assert message.text == mail_cli.TEST_TEXT
    assert key == "esp-test-v1-a"
    assert payload(result)["status"] == "accepted"
    assert payload(result)["receipt_verified"] is False
    assert "operator@example.test" not in result.output


@pytest.mark.parametrize("status", ["disabled", "unconfigured", "rejected", "unknown", "retryable"])
def test_unaccepted_dispatch_is_nonzero_and_never_claims_receipt(app, monkeypatch, status):
    monkeypatch.setattr(ResendMailer, "send", lambda *args, **kwargs: SimpleNamespace(
        status=status, to_dict=lambda: {"status": status, "code": "safe_code"},
    ))
    result = app.test_cli_runner().invoke(args=[
        "mail-test", "--to", "operator@example.test", "--delivery-key", "esp-test-v1-a", "--send",
    ])
    assert result.exit_code == 1
    assert payload(result)["status"] == status
    assert payload(result)["receipt_verified"] is False


def test_unexpected_error_never_exposes_exception_text(app, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("re_private_key recipient@example.test")

    monkeypatch.setattr(ResendMailer, "configuration_status", fail)
    result = app.test_cli_runner().invoke(args=["mail-check"])
    assert result.exit_code == 1
    assert payload(result) == {"status": "error", "code": "mail_service_error"}
    assert "re_private_key" not in result.output
    assert "recipient@example.test" not in result.output
