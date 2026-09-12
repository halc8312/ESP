"""Opt-in Resend transport, with no automatic business events or retries.

The caller owns recipient authorization and durable delivery state. Persist one
idempotency key and an immutable payload per logical email, reuse both when
retrying, and reconcile ambiguous outcomes before Resend's 24-hour key retention
expires. An ``accepted`` result means API acceptance, never inbox delivery.
"""

from dataclasses import dataclass, field
from collections.abc import Mapping
import os
import re
from typing import Any
from uuid import UUID

import requests


_API_URL = "https://api.resend.com/emails"
_TIMEOUT = (3.05, 10.0)
_USER_AGENT = "ESP-Mail/1.0"
_DEFAULT_SENDER = "noreply@jp-items.com"
_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}
_LOCAL_PART = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def _plain_email_valid(value: Any) -> bool:
    """Accept one conventional ASCII mailbox, without display names or lists."""
    if not isinstance(value, str) or not 3 <= len(value) <= 254:
        return False
    if not value.isascii() or value.count("@") != 1:
        return False
    local, domain = value.split("@")
    return bool(
        1 <= len(local) <= 64
        and _LOCAL_PART.fullmatch(local)
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
        and len(domain) <= 253
        and "." in domain
        and all(_DOMAIN_LABEL.fullmatch(label) for label in domain.split("."))
    )


@dataclass(frozen=True)
class MailMessage:
    to: str = field(repr=False)
    subject: str = field(repr=False)
    text: str = field(repr=False)
    html: str | None = field(default=None, repr=False)


def validate_message(message: MailMessage, idempotency_key: str) -> tuple[str, ...]:
    """Validate without network access or reflecting any supplied content."""
    errors = []
    if not isinstance(message, MailMessage):
        errors.append("invalid_message")
    else:
        if not _plain_email_valid(message.to):
            errors.append("invalid_recipient")
        subject = message.subject
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in subject)
        ):
            errors.append("invalid_subject")
        if (
            not isinstance(message.text, str)
            or not message.text.strip()
            or len(message.text) > 100_000
            or "\x00" in message.text
        ):
            errors.append("invalid_text")
        if message.html is not None and (
            not isinstance(message.html, str)
            or not message.html.strip()
            or len(message.html) > 200_000
            or "\x00" in message.html
        ):
            errors.append("invalid_html")
    if (
        not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= 256
        or any(not 33 <= ord(character) <= 126 for character in idempotency_key)
    ):
        errors.append("invalid_idempotency_key")
    return tuple(errors)


@dataclass(frozen=True)
class MailSettings:
    api_key: str = field(default="", repr=False)
    sender: str = field(default=_DEFAULT_SENDER, repr=False)
    enabled: bool = field(default=False, repr=False)
    error_codes: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MailSettings":
        source = os.environ if env is None else env
        raw_enabled = source.get("MAIL_ENABLED", "false")
        normalized = raw_enabled.strip().lower() if isinstance(raw_enabled, str) else ""
        errors = () if normalized in _TRUE_VALUES | _FALSE_VALUES else ("invalid_mail_enabled",)
        return cls(
            api_key=source.get("RESEND_API_KEY", ""),
            sender=source.get("MAIL_FROM", _DEFAULT_SENDER),
            enabled=normalized in _TRUE_VALUES,
            error_codes=errors,
        )


@dataclass(frozen=True)
class MailResult:
    status: str
    code: str
    http_status: int | None = None
    message_id: str | None = None
    retry_after_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in (
                ("status", self.status),
                ("code", self.code),
                ("http_status", self.http_status),
                ("message_id", self.message_id),
                ("retry_after_seconds", self.retry_after_seconds),
            )
            if value is not None
        }


def _retry_after(response: Any) -> int | None:
    value = response.headers.get("Retry-After")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value.strip()):
        seconds = int(value.strip())
        if seconds <= 2_147_483_647:
            return seconds
    return None


class ResendMailer:
    """A single-attempt transport; ``session`` is an injectable test seam.

    The default transport uses a fresh Session without retries or inherited
    proxy/netrc credentials. A caller supplying a session must likewise disable
    automatic POST retries. Neither configuration inspection nor construction
    contacts Resend.
    """

    def __init__(self, settings: MailSettings | None = None, *, session: Any = None):
        self._settings = settings if settings is not None else MailSettings.from_env()
        self._session = session

    def configuration_status(self) -> dict[str, Any]:
        settings = self._settings
        key_configured = isinstance(settings.api_key, str) and bool(settings.api_key)
        sender_valid = _plain_email_valid(settings.sender)
        errors = []
        # Do not reflect custom error values from a caller-created settings object.
        if settings.error_codes or not isinstance(settings.enabled, bool):
            errors.append("invalid_mail_enabled")
        if not sender_valid:
            errors.append("invalid_sender")
        if key_configured and (
            len(settings.api_key) > 4096
            or any(not 33 <= ord(character) <= 126 for character in settings.api_key)
        ):
            errors.append("invalid_api_key")
        elif not isinstance(settings.api_key, str):
            errors.append("invalid_api_key")
        enabled = settings.enabled is True and not settings.error_codes
        if errors:
            status = "configuration_error"
        elif not enabled:
            status = "disabled"
        elif not key_configured:
            status = "unconfigured"
            errors.append("api_key_missing")
        else:
            status = "ready"
        return {
            "status": status,
            "enabled": enabled,
            "key_configured": key_configured,
            "sender_valid": sender_valid,
            "error_codes": errors,
        }

    def send(self, message: MailMessage, *, idempotency_key: str) -> MailResult:
        configuration = self.configuration_status()
        if configuration["status"] == "disabled":
            return MailResult("disabled", "mail_disabled")
        if configuration["status"] != "ready":
            return MailResult(configuration["status"], configuration["error_codes"][0])
        errors = validate_message(message, idempotency_key)
        if errors:
            return MailResult("rejected", errors[0])

        payload = {
            "from": self._settings.sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html is not None:
            payload["html"] = message.html
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Idempotency-Key": idempotency_key,
        }
        # Exceptions and provider bodies can contain credentials or mail content.
        # Return fixed codes, never exception messages, request objects, or bodies.
        try:
            if self._session is None:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.post(
                        _API_URL, json=payload, headers=headers,
                        timeout=_TIMEOUT, allow_redirects=False,
                    )
            else:
                response = self._session.post(
                    _API_URL, json=payload, headers=headers,
                    timeout=_TIMEOUT, allow_redirects=False,
                )
            return self._classify_response(response)
        except requests.Timeout:
            return MailResult("unknown", "request_timeout")
        except Exception:
            return MailResult("unknown", "transport_error")

    @staticmethod
    def _classify_response(response: Any) -> MailResult:
        status = response.status_code
        if type(status) is not int or not 100 <= status <= 599:
            return MailResult("unknown", "invalid_response")
        if status >= 500:
            return MailResult("unknown", "provider_unavailable", http_status=status)
        try:
            body = response.json()
        except Exception:
            body = None
        if 200 <= status < 300:
            identifier = body.get("id") if isinstance(body, dict) else None
            try:
                if not isinstance(identifier, str) or len(identifier) > 36:
                    raise ValueError
                message_id = str(UUID(identifier))
            except (ValueError, AttributeError):
                return MailResult("unknown", "invalid_response", http_status=status)
            return MailResult("accepted", "api_accepted", http_status=status, message_id=message_id)

        error = body.get("name") if isinstance(body, dict) else None
        if status == 409 and error == "concurrent_idempotent_requests":
            return MailResult("retryable", "request_in_progress", status, retry_after_seconds=_retry_after(response))
        if status == 409 and error == "invalid_idempotent_request":
            return MailResult("rejected", "idempotency_mismatch", status)
        if status == 429:
            if error == "rate_limit_exceeded":
                return MailResult("retryable", "rate_limited", status, retry_after_seconds=_retry_after(response))
            if error == "daily_quota_exceeded":
                return MailResult("rejected", "daily_quota_exceeded", status)
            if error == "monthly_quota_exceeded":
                return MailResult("rejected", "monthly_quota_exceeded", status)
        if status in (401, 403):
            return MailResult("rejected", "authorization_rejected", status)
        if 300 <= status < 400:
            return MailResult("rejected", "unexpected_redirect", status)
        if 400 <= status < 500:
            return MailResult("rejected", "provider_rejected", status)
        return MailResult("unknown", "invalid_response", status)
