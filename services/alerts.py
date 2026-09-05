import json
import logging
import os
import re
import threading
import time
from datetime import datetime, UTC
from typing import Any
from urllib import request
from urllib.parse import urlparse

from utils.env_helpers import env_int as _env_int


logger = logging.getLogger("alerts")

_DISCORD_USERNAME = "ESP Alerts"
_DEFAULT_ALERT_USER_AGENT = "ESP-Alerts/1.0 (+https://github.com/halc8312/ESP)"
_DISCORD_COLOR_BY_SEVERITY = {
    "info": 3447003,
    "warning": 16776960,
    "error": 15158332,
    "critical": 10038562,
}


class AlertDispatcher:
    def __init__(self, sender=None):
        self._sender = sender or self._post_json
        self._lock = threading.Lock()
        self._last_sent_by_key: dict[str, float] = {}
        self._global_sent_at: list[float] = []
        self._in_flight_keys: set[str] = set()

    @property
    def selector_webhook_url(self) -> str:
        return str(os.environ.get("SELECTOR_ALERT_WEBHOOK_URL", "") or "").strip()

    @property
    def selector_cooldown_seconds(self) -> int:
        return max(0, _env_int("SELECTOR_ALERT_COOLDOWN_SECONDS", 900))

    @property
    def selector_max_per_window(self) -> int:
        return max(1, _env_int("SELECTOR_ALERT_MAX_PER_WINDOW", 10))

    @property
    def selector_window_seconds(self) -> int:
        return max(1, _env_int("SELECTOR_ALERT_WINDOW_SECONDS", 300))

    @property
    def operational_webhook_url(self) -> str:
        return str(os.environ.get("OPERATIONAL_ALERT_WEBHOOK_URL", "") or "").strip()

    @property
    def operational_webhook_configured(self) -> bool:
        return bool(self.operational_webhook_url)

    @property
    def operational_cooldown_seconds(self) -> int:
        return max(0, _env_int("OPERATIONAL_ALERT_COOLDOWN_SECONDS", 900))

    @property
    def operational_max_per_window(self) -> int:
        return max(1, _env_int("OPERATIONAL_ALERT_MAX_PER_WINDOW", 10))

    @property
    def operational_window_seconds(self) -> int:
        return max(1, _env_int("OPERATIONAL_ALERT_WINDOW_SECONDS", 300))

    @property
    def scrape_webhook_url(self) -> str:
        explicit = str(os.environ.get("SCRAPE_ALERT_WEBHOOK_URL", "") or "").strip()
        if explicit:
            return explicit
        return self.selector_webhook_url or self.operational_webhook_url

    @property
    def scrape_webhook_configured(self) -> bool:
        return bool(self.scrape_webhook_url)

    @property
    def scrape_cooldown_seconds(self) -> int:
        return max(0, _env_int("SCRAPE_ALERT_COOLDOWN_SECONDS", self.selector_cooldown_seconds))

    @property
    def scrape_max_per_window(self) -> int:
        return max(1, _env_int("SCRAPE_ALERT_MAX_PER_WINDOW", self.selector_max_per_window))

    @property
    def scrape_window_seconds(self) -> int:
        return max(1, _env_int("SCRAPE_ALERT_WINDOW_SECONDS", self.selector_window_seconds))

    def _dispatch_rate_limited(
        self,
        *,
        webhook_url: str,
        cooldown_seconds: int,
        max_per_window: int,
        window_seconds: int,
        key: str,
        payload: dict[str, Any],
        log_label: str,
    ) -> bool:
        return self._dispatch_rate_limited_result(
            webhook_url=webhook_url,
            cooldown_seconds=cooldown_seconds,
            max_per_window=max_per_window,
            window_seconds=window_seconds,
            key=key,
            payload=payload,
            log_label=log_label,
        )["status"] == "delivered"

    @staticmethod
    def _safe_log_token(value: Any) -> str:
        token = str(value or "")
        return token if re.fullmatch(r"[A-Za-z0-9_]{1,80}", token) else "unknown"

    def _dispatch_result(
        self,
        *,
        status: str,
        payload: dict[str, Any],
        log_label: str,
        error_type: str | None = None,
    ) -> dict[str, str | None]:
        # Never log the webhook URL, dedupe key, payload, or exception message.
        log = logger.warning if status == "failed" else logger.debug
        log(
            "Alert dispatch category=%s event=%s status=%s error_type=%s",
            self._safe_log_token(log_label.lower()),
            self._safe_log_token(payload.get("event_type")),
            status,
            error_type or "none",
        )
        return {"status": status, "error_type": error_type}

    def _dispatch_rate_limited_result(
        self,
        *,
        webhook_url: str,
        cooldown_seconds: int,
        max_per_window: int,
        window_seconds: int,
        key: str,
        payload: dict[str, Any],
        log_label: str,
    ) -> dict[str, str | None]:
        """Return delivery disposition without treating a failed send as sent.

        ``delivered`` means the webhook HTTP request was accepted, not that a
        person received/read it. The injected sender must raise on rejection.
        Cooldown, rate limiting, and in-flight reservations are process-local.
        """
        if not webhook_url:
            return self._dispatch_result(status="unconfigured", payload=payload, log_label=log_label)

        now = time.monotonic()
        suppressed = None
        with self._lock:
            window_start = now - window_seconds
            # A shorter-window category must not erase another category's
            # longer-window history from the shared rate limit.
            retention_seconds = max(
                window_seconds,
                self.selector_window_seconds,
                self.operational_window_seconds,
                self.scrape_window_seconds,
            )
            self._global_sent_at = [ts for ts in self._global_sent_at if ts >= now - retention_seconds]
            sent_in_window = sum(ts >= window_start for ts in self._global_sent_at)
            last_sent = self._last_sent_by_key.get(key)
            if key in self._in_flight_keys:
                suppressed = "in_flight"
            elif last_sent is not None and (now - last_sent) < cooldown_seconds:
                suppressed = "cooldown"
            elif sent_in_window + len(self._in_flight_keys) >= max_per_window:
                suppressed = "rate_limited"
            else:
                # Reservations prevent concurrent sends from exceeding the
                # budget; only successful completions consume that budget.
                self._in_flight_keys.add(key)

        if suppressed:
            return self._dispatch_result(status=suppressed, payload=payload, log_label=log_label)

        status = "failed"
        error_type = None
        try:
            self._sender(webhook_url, payload)
            status = "delivered"
        except Exception as exc:
            error_type = self._safe_log_token(type(exc).__name__)
        finally:
            with self._lock:
                self._in_flight_keys.discard(key)
                if status == "delivered":
                    accepted_at = time.monotonic()
                    self._last_sent_by_key[key] = accepted_at
                    self._global_sent_at.append(accepted_at)
        return self._dispatch_result(
            status=status, payload=payload, log_label=log_label, error_type=error_type
        )

    def notify_selector_issue(
        self,
        *,
        event_type: str,
        site: str,
        page_type: str,
        field: str,
        severity: str = "warning",
        message: str = "",
        details: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> bool:
        key = dedupe_key or f"selector:{event_type}:{site}:{page_type}:{field}"

        payload = {
            "text": f"[selector-healer][{severity}] {event_type} {site}/{page_type}/{field}",
            "category": "selector",
            "event_type": event_type,
            "severity": severity,
            "site": site,
            "page_type": page_type,
            "field": field,
            "message": message,
            "details": details or {},
            "dedupe_key": key,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return self._dispatch_rate_limited(
            webhook_url=self.selector_webhook_url,
            cooldown_seconds=self.selector_cooldown_seconds,
            max_per_window=self.selector_max_per_window,
            window_seconds=self.selector_window_seconds,
            key=key,
            payload=payload,
            log_label="Selector",
        )

    def notify_operational_issue(
        self,
        *,
        event_type: str,
        component: str,
        severity: str = "warning",
        message: str = "",
        details: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> bool:
        return self.notify_operational_issue_result(
            event_type=event_type,
            component=component,
            severity=severity,
            message=message,
            details=details,
            dedupe_key=dedupe_key,
        )["status"] == "delivered"

    def notify_operational_issue_result(
        self,
        *,
        event_type: str,
        component: str,
        severity: str = "warning",
        message: str = "",
        details: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> dict[str, str | None]:
        key = dedupe_key or f"operational:{event_type}:{component}"
        payload = {
            "text": f"[operations][{severity}] {event_type} {component}",
            "category": "operational",
            "event_type": event_type,
            "severity": severity,
            "component": component,
            "message": message,
            "details": details or {},
            "dedupe_key": key,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return self._dispatch_rate_limited_result(
            webhook_url=self.operational_webhook_url,
            cooldown_seconds=self.operational_cooldown_seconds,
            max_per_window=self.operational_max_per_window,
            window_seconds=self.operational_window_seconds,
            key=key,
            payload=payload,
            log_label="Operational",
        )

    def notify_scrape_issue(
        self,
        *,
        event_type: str,
        site: str,
        page_type: str,
        field: str = "",
        severity: str = "warning",
        message: str = "",
        details: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> bool:
        return self.notify_scrape_issue_result(
            event_type=event_type,
            site=site,
            page_type=page_type,
            field=field,
            severity=severity,
            message=message,
            details=details,
            dedupe_key=dedupe_key,
        )["status"] == "delivered"

    def notify_scrape_issue_result(
        self,
        *,
        event_type: str,
        site: str,
        page_type: str,
        field: str = "",
        severity: str = "warning",
        message: str = "",
        details: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> dict[str, str | None]:
        key = dedupe_key or f"scrape:{event_type}:{site}:{page_type}:{field or 'general'}"
        payload = {
            "text": f"[scrape][{severity}] {event_type} {site}/{page_type}/{field or 'general'}",
            "category": "scrape",
            "event_type": event_type,
            "severity": severity,
            "site": site,
            "page_type": page_type,
            "field": field,
            "message": message,
            "details": details or {},
            "dedupe_key": key,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return self._dispatch_rate_limited_result(
            webhook_url=self.scrape_webhook_url,
            cooldown_seconds=self.scrape_cooldown_seconds,
            max_per_window=self.scrape_max_per_window,
            window_seconds=self.scrape_window_seconds,
            key=key,
            payload=payload,
            log_label="Scrape",
        )

    @staticmethod
    def _truncate_text(value: Any, *, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _is_discord_webhook_url(url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if host not in {"discord.com", "www.discord.com", "discordapp.com", "www.discordapp.com"}:
            return False
        return path.startswith("/api/webhooks/")

    @classmethod
    def _build_discord_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        title = cls._truncate_text(
            payload.get("text")
            or f"[{payload.get('category') or 'alert'}][{payload.get('severity') or 'warning'}] {payload.get('event_type') or 'event'}",
            limit=2000,
        )
        message = cls._truncate_text(payload.get("message"), limit=4000)
        details = payload.get("details") or {}
        target_parts = [
            str(payload.get("site") or "").strip(),
            str(payload.get("page_type") or "").strip(),
            str(payload.get("field") or "").strip(),
        ]
        target = "/".join(part for part in target_parts if part)

        embed_fields: list[dict[str, Any]] = []
        for name, value in (
            ("Category", payload.get("category")),
            ("Event", payload.get("event_type")),
            ("Severity", payload.get("severity")),
            ("Target", target or payload.get("component")),
            ("Dedupe", payload.get("dedupe_key")),
        ):
            text = cls._truncate_text(value, limit=1024)
            if text:
                embed_fields.append({"name": name, "value": text, "inline": name != "Dedupe"})

        if details:
            details_text = cls._truncate_text(json.dumps(details, ensure_ascii=False, sort_keys=True), limit=1024)
            embed_fields.append({"name": "Details", "value": details_text, "inline": False})

        embed: dict[str, Any] = {
            "title": cls._truncate_text(title, limit=256),
            "color": _DISCORD_COLOR_BY_SEVERITY.get(str(payload.get("severity") or "warning").lower(), 16776960),
            "fields": embed_fields[:25],
        }
        if message:
            embed["description"] = cls._truncate_text(message, limit=4096)
        if payload.get("timestamp"):
            embed["timestamp"] = payload["timestamp"]

        return {
            "content": title,
            "username": _DISCORD_USERNAME,
            "allowed_mentions": {"parse": []},
            "embeds": [embed],
        }

    @classmethod
    def _prepare_outbound_payload(cls, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if cls._is_discord_webhook_url(url):
            return cls._build_discord_payload(payload)
        return payload

    @staticmethod
    def _build_request_headers() -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": _DEFAULT_ALERT_USER_AGENT,
        }

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> None:
        outbound_payload = AlertDispatcher._prepare_outbound_payload(url, payload)
        body = json.dumps(outbound_payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers=AlertDispatcher._build_request_headers(),
            method="POST",
        )
        with request.urlopen(req, timeout=5):
            return None


_dispatcher = AlertDispatcher()


def get_alert_dispatcher() -> AlertDispatcher:
    return _dispatcher
