import json
import logging
import os
import threading
import time
from datetime import datetime, UTC
from typing import Any
from urllib import request
from urllib.parse import urlparse


logger = logging.getLogger("alerts")

_DISCORD_USERNAME = "ESP Alerts"
_DEFAULT_ALERT_USER_AGENT = "ESP-Alerts/1.0 (+https://github.com/halc8312/ESP)"
_DISCORD_COLOR_BY_SEVERITY = {
    "info": 3447003,
    "warning": 16776960,
    "error": 15158332,
    "critical": 10038562,
}


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class AlertDispatcher:
    def __init__(self, sender=None):
        self._sender = sender or self._post_json
        self._lock = threading.Lock()
        self._last_sent_by_key: dict[str, float] = {}
        self._global_sent_at: list[float] = []

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
    def operational_cooldown_seconds(self) -> int:
        return max(0, _env_int("OPERATIONAL_ALERT_COOLDOWN_SECONDS", 900))

    @property
    def operational_max_per_window(self) -> int:
        return max(1, _env_int("OPERATIONAL_ALERT_MAX_PER_WINDOW", 10))

    @property
    def operational_window_seconds(self) -> int:
        return max(1, _env_int("OPERATIONAL_ALERT_WINDOW_SECONDS", 300))

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
        if not webhook_url:
            return False

        now = time.monotonic()
        with self._lock:
            window_start = now - window_seconds
            self._global_sent_at = [ts for ts in self._global_sent_at if ts >= window_start]

            if len(self._global_sent_at) >= max_per_window:
                logger.debug("%s alert suppressed by global rate limit: %s", log_label, key)
                return False

            last_sent = self._last_sent_by_key.get(key)
            if last_sent is not None and (now - last_sent) < cooldown_seconds:
                logger.debug("%s alert suppressed by cooldown: %s", log_label, key)
                return False

            self._last_sent_by_key[key] = now
            self._global_sent_at.append(now)

        try:
            self._sender(webhook_url, payload)
            return True
        except Exception as exc:
            logger.warning("%s alert dispatch failed for %s: %s", log_label, key, exc)
            return False

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
        return self._dispatch_rate_limited(
            webhook_url=self.operational_webhook_url,
            cooldown_seconds=self.operational_cooldown_seconds,
            max_per_window=self.operational_max_per_window,
            window_seconds=self.operational_window_seconds,
            key=key,
            payload=payload,
            log_label="Operational",
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
