import json

import pytest
import requests

from services.mail_service import (
    MailMessage,
    MailSettings,
    ResendMailer,
    validate_message,
)


_MESSAGE_ID = "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"
_KEY = "re_test_secret_not_a_real_api_key"


class FakeSession:
    def __init__(self, status=200, body=None, *, headers=None, error=None):
        self.calls = []
        self.error = error
        self.response = requests.Response()
        self.response.status_code = status
        self.response.headers.update(headers or {})
        self.response._content = json.dumps(body if body is not None else {"id": _MESSAGE_ID}).encode()

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Mail transport tests must never use the network")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def message():
    return MailMessage("delivered@resend.dev", "ESP テスト", "テスト本文です。\nSecond line.")


@pytest.fixture
def settings():
    return MailSettings.from_env({"MAIL_ENABLED": "true", "RESEND_API_KEY": _KEY})


def test_default_is_disabled_even_with_key_and_inspection_sends_nothing(message):
    session = FakeSession()
    mailer = ResendMailer(MailSettings.from_env({"RESEND_API_KEY": _KEY}), session=session)
    assert mailer.configuration_status() == {
        "status": "disabled", "enabled": False, "key_configured": True,
        "sender_valid": True, "error_codes": [],
    }
    assert mailer.send(message, idempotency_key="test/one").to_dict() == {
        "status": "disabled", "code": "mail_disabled",
    }
    assert not session.calls


def test_empty_mapping_does_not_read_environment(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", _KEY)
    assert ResendMailer(MailSettings.from_env({})).configuration_status()["status"] == "disabled"
    assert ResendMailer().configuration_status()["status"] == "ready"


@pytest.mark.parametrize("value", ["", "perhaps", "enabled", "2", None, True])
def test_invalid_enabled_fails_closed_without_echoing_value(value, message):
    session = FakeSession()
    mailer = ResendMailer(MailSettings.from_env({"MAIL_ENABLED": value, "RESEND_API_KEY": _KEY}), session=session)
    assert mailer.configuration_status()["status"] == "configuration_error"
    assert mailer.configuration_status()["error_codes"] == ["invalid_mail_enabled"]
    assert mailer.send(message, idempotency_key="test/one").status == "configuration_error"
    assert not session.calls


@pytest.mark.parametrize("value", ["false", "0", "NO", " off "])
def test_explicit_disabled_values(value):
    assert ResendMailer(MailSettings.from_env({"MAIL_ENABLED": value})).configuration_status()["status"] == "disabled"


def test_enabled_requires_key_and_valid_sender(message):
    session = FakeSession()
    no_key = ResendMailer(MailSettings.from_env({"MAIL_ENABLED": "true"}), session=session)
    assert no_key.configuration_status()["status"] == "unconfigured"
    assert no_key.send(message, idempotency_key="test/one").to_dict() == {
        "status": "unconfigured", "code": "api_key_missing",
    }
    invalid_sender = ResendMailer(MailSettings.from_env({
        "MAIL_ENABLED": "true", "MAIL_FROM": "noreply@jp-items.com\r\nBcc: victim@example.com", "RESEND_API_KEY": _KEY,
    }), session=session)
    assert invalid_sender.configuration_status()["error_codes"] == ["invalid_sender"]
    assert invalid_sender.send(message, idempotency_key="test/one").code == "invalid_sender"
    assert not session.calls


@pytest.mark.parametrize("key", [" ", "re_key\r\nInjected: yes", "é", 123, "x" * 4097])
def test_invalid_api_key_never_reaches_network(key, message):
    session = FakeSession()
    mailer = ResendMailer(MailSettings(api_key=key, enabled=True), session=session)
    assert mailer.send(message, idempotency_key="test/one").code == "invalid_api_key"
    assert not session.calls


def test_send_exact_contract_and_accepted_is_not_delivery(settings, message):
    session = FakeSession()
    result = ResendMailer(settings, session=session).send(message, idempotency_key="test/one")
    assert result.to_dict() == {
        "status": "accepted", "code": "api_accepted", "http_status": 200, "message_id": _MESSAGE_ID,
    }
    assert len(session.calls) == 1
    args, kwargs = session.calls[0]
    assert args == ("https://api.resend.com/emails",)
    assert kwargs["json"] == {
        "from": "noreply@jp-items.com", "to": [message.to], "subject": message.subject, "text": message.text,
    }
    assert kwargs["headers"] == {
        "Authorization": f"Bearer {_KEY}", "Content-Type": "application/json",
        "User-Agent": "ESP-Mail/1.0", "Idempotency-Key": "test/one",
    }
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (3.05, 10.0)


def test_optional_html_is_transmitted_without_mutating_payload(settings):
    session = FakeSession()
    message = MailMessage("delivered@resend.dev", "Subject", "Plain text", "<p>HTML</p>")
    mailer = ResendMailer(settings, session=session)
    mailer.send(message, idempotency_key="event/one")
    mailer.send(message, idempotency_key="event/one")
    assert session.calls[0][1] == session.calls[1][1]
    assert session.calls[0][1]["json"]["html"] == "<p>HTML</p>"


@pytest.mark.parametrize("recipient", [
    "Name <one@example.com>", "one@example.com,two@example.com", "one@example.com\n",
    ["one@example.com"], "one@localhost", ".one@example.com", "one..two@example.com",
    "one@-example.com", "one@example..com", "one@example.com;two@example.com",
])
def test_single_plain_recipient_validation_blocks_network(recipient, settings):
    session = FakeSession()
    message = MailMessage(recipient, "Subject", "Body")
    assert validate_message(message, "test/key") == ("invalid_recipient",)
    assert ResendMailer(settings, session=session).send(message, idempotency_key="test/key").code == "invalid_recipient"
    assert not session.calls


@pytest.mark.parametrize("changes,code", [
    ({"subject": "Subject\r\nBcc: other@example.com"}, "invalid_subject"),
    ({"subject": " "}, "invalid_subject"),
    ({"subject": "x" * 201}, "invalid_subject"),
    ({"subject": None}, "invalid_subject"),
    ({"text": ""}, "invalid_text"),
    ({"text": "x" * 100_001}, "invalid_text"),
    ({"text": "body\x00"}, "invalid_text"),
    ({"text": 12}, "invalid_text"),
    ({"html": " "}, "invalid_html"),
    ({"html": "x" * 200_001}, "invalid_html"),
    ({"html": []}, "invalid_html"),
])
def test_message_shape_and_size(changes, code, settings):
    values = {"to": "one@example.com", "subject": "Subject", "text": "Body"}
    values.update(changes)
    message = MailMessage(**values)
    session = FakeSession()
    assert validate_message(message, "test/key") == (code,)
    assert ResendMailer(settings, session=session).send(message, idempotency_key="test/key").code == code
    assert not session.calls


@pytest.mark.parametrize("key", ["", "x" * 257, "has space", "has\nnewline", "日本語", None])
def test_idempotency_key_required_and_never_generated_silently(key, settings, message):
    session = FakeSession()
    assert validate_message(message, key) == ("invalid_idempotency_key",)
    assert ResendMailer(settings, session=session).send(message, idempotency_key=key).code == "invalid_idempotency_key"
    assert not session.calls


@pytest.mark.parametrize("http_status,name,status,code", [
    (409, "concurrent_idempotent_requests", "retryable", "request_in_progress"),
    (409, "invalid_idempotent_request", "rejected", "idempotency_mismatch"),
    (429, "rate_limit_exceeded", "retryable", "rate_limited"),
    (429, "daily_quota_exceeded", "rejected", "daily_quota_exceeded"),
    (429, "monthly_quota_exceeded", "rejected", "monthly_quota_exceeded"),
    (429, "unexpected_provider_name", "rejected", "provider_rejected"),
    (400, "validation_error", "rejected", "provider_rejected"),
    (401, "missing_api_key", "rejected", "authorization_rejected"),
    (403, "validation_error", "rejected", "authorization_rejected"),
    (422, "missing_required_field", "rejected", "provider_rejected"),
    (302, "redirect", "rejected", "unexpected_redirect"),
    (500, "application_error", "unknown", "provider_unavailable"),
    (503, "service_unavailable", "unknown", "provider_unavailable"),
])
def test_provider_errors_are_classified_without_retry(http_status, name, status, code, settings, message):
    session = FakeSession(http_status, {"name": name, "message": f"Private {_KEY} {message.text}"}, headers={"Retry-After": "120"})
    result = ResendMailer(settings, session=session).send(message, idempotency_key="test/one")
    assert result.status == status
    assert result.code == code
    assert result.http_status == http_status
    assert result.retry_after_seconds == (120 if status == "retryable" else None)
    assert _KEY not in repr(result)
    assert message.text not in json.dumps(result.to_dict())
    assert len(session.calls) == 1


@pytest.mark.parametrize("retry_after", ["invalid", "-1", "NaN", "infinity", "999999999999999999999999999"])
def test_untrusted_retry_after_is_not_reflected(retry_after, settings, message):
    session = FakeSession(429, {"name": "rate_limit_exceeded"}, headers={"Retry-After": retry_after})
    result = ResendMailer(settings, session=session).send(message, idempotency_key="test/one")
    assert result.status == "retryable"
    assert result.retry_after_seconds is None


@pytest.mark.parametrize("body", [{}, {"id": "not-a-uuid"}, {"id": None}, {"id": {"secret": _KEY}}, ["id", _MESSAGE_ID]])
def test_malformed_success_is_unknown_never_accepted(body, settings, message):
    session = FakeSession(body=body)
    result = ResendMailer(settings, session=session).send(message, idempotency_key="test/one")
    assert result.to_dict() == {"status": "unknown", "code": "invalid_response", "http_status": 200}
    assert len(session.calls) == 1


def test_non_json_success_is_unknown(settings, message):
    session = FakeSession()
    session.response._content = f"<html>{_KEY}</html>".encode()
    result = ResendMailer(settings, session=session).send(message, idempotency_key="test/one")
    assert result.status == "unknown"
    assert result.code == "invalid_response"


@pytest.mark.parametrize("error_type,code", [
    (requests.Timeout, "request_timeout"),
    (requests.ConnectionError, "transport_error"),
    (RuntimeError, "transport_error"),
])
def test_transport_exception_never_exposes_secret_or_retries(error_type, code, settings, message, caplog):
    session = FakeSession(error=error_type(f"{_KEY} {message.to} {message.text}"))
    result = ResendMailer(settings, session=session).send(message, idempotency_key="test/one")
    assert result.to_dict() == {"status": "unknown", "code": code}
    assert len(session.calls) == 1
    for private in (_KEY, message.to, message.text):
        assert private not in repr(result)
        assert private not in caplog.text


def test_sensitive_objects_and_config_output_have_safe_repr(settings, message):
    mailer = ResendMailer(settings)
    output = repr(settings) + repr(message) + repr(mailer) + json.dumps(mailer.configuration_status())
    for private in (_KEY, message.to, message.text, message.subject, settings.sender):
        assert private not in output
    assert _KEY not in repr(MailSettings(enabled=_KEY, error_codes=(_KEY,)))


def test_default_session_does_not_inherit_proxies_or_netrc(monkeypatch, settings, message):
    fake = FakeSession()
    fake.trust_env = True

    class SessionContext:
        def __enter__(self):
            return fake

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(requests, "Session", SessionContext)
    result = ResendMailer(settings).send(message, idempotency_key="test/one")
    assert result.status == "accepted"
    assert fake.trust_env is False
    assert len(fake.calls) == 1
