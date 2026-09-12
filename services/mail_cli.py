"""Operator commands for Resend configuration and one explicit test message.

No command prints an API key, recipient, message body, or remote error text.
Neither configuration inspection nor the default test preview uses the network.
"""
from __future__ import annotations

import json

import click

from services.mail_service import MailMessage, ResendMailer, validate_message


# Versioned, immutable content makes retrying the same test delivery key safe.
# A future text change must use a new test delivery key.
TEST_SUBJECT = "ESP メール送信確認"
TEST_TEXT = (
    "ESPからのメール送信確認です。\n"
    "このメールの受信を、送信テストを依頼した管理者へお知らせください。\n"
    "このメールにはパスワードや商品情報は含まれていません。\n"
    "送信確認 v1\n"
)


def _emit(payload):
    click.echo(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def register_mail_cli_commands(app):
    @app.cli.command("mail-check")
    def mail_check():
        """Inspect local mail settings; never authenticate or send a message."""
        try:
            configuration = ResendMailer().configuration_status()
        except Exception:
            _emit({"status": "error", "code": "mail_service_error"})
            raise click.exceptions.Exit(1) from None
        _emit({**configuration, "network_used": False, "provider_verified": False})
        if configuration["status"] != "ready":
            raise click.exceptions.Exit(2)

    @app.cli.command("mail-test")
    @click.option("--to", "recipient", required=True, help="Recipient designated for this test.")
    @click.option(
        "--delivery-key", required=True,
        help="Stable key for this one message; reuse unchanged for retries within 24 hours.",
    )
    @click.option("--send", is_flag=True, help="Send once. Without this flag, only inspect local settings.")
    def mail_test(recipient, delivery_key, send):
        """Preview, or explicitly send, the fixed ESP test message to one address."""
        message = MailMessage(to=recipient, subject=TEST_SUBJECT, text=TEST_TEXT)
        errors = validate_message(message, delivery_key)
        if errors:
            _emit({"status": "invalid_message", "error_codes": list(errors), "network_used": False})
            raise click.exceptions.Exit(2)

        try:
            mailer = ResendMailer()
            if not send:
                configuration = mailer.configuration_status()
                _emit({
                    "status": "dry_run", "network_used": False,
                    "provider_verified": False, "configuration": configuration,
                })
                exit_code = 0 if configuration["status"] == "ready" else 2
            else:
                result = mailer.send(message, idempotency_key=delivery_key)
                _emit({**result.to_dict(), "receipt_verified": False})
                exit_code = 0 if result.status == "accepted" else 1
        except Exception:
            _emit({"status": "error", "code": "mail_service_error"})
            raise click.exceptions.Exit(1) from None
        if exit_code:
            raise click.exceptions.Exit(exit_code)
