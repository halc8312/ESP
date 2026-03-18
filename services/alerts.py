import json
import logging
import os
import threading
import time
from datetime import datetime, UTC
from typing import Any
from urllib import request


logger = logging.getLogger("alerts")


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
    def webhook_url(self) -> str:
        return str(os.environ.get("SELECTOR_ALERT_WEBHOOK_URL", "") or "").strip()

    @property
    def cooldown_seconds(self) -> int:
        return max(0, _env_int("SELECTOR_ALERT_COOLDOWN_SECONDS", 900))

    @property
    def max_per_window(self) -> int:
        return max(1, _env_int("SELECTOR_ALERT_MAX_PER_WINDOW", 10))

    @property
    def window_seconds(self) -> int:
        return max(1, _env_int("SELECTOR_ALERT_WINDOW_SECONDS", 300))

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
        webhook_url = self.webhook_url
        if not webhook_url:
            return False

        now = time.monotonic()
        key = dedupe_key or f"selector:{event_type}:{site}:{page_type}:{field}"

        with self._lock:
            window_start = now - self.window_seconds
            self._global_sent_at = [ts for ts in self._global_sent_at if ts >= window_start]

            if len(self._global_sent_at) >= self.max_per_window:
                logger.debug("Selector alert suppressed by global rate limit: %s", key)
                return False

            last_sent = self._last_sent_by_key.get(key)
            if last_sent is not None and (now - last_sent) < self.cooldown_seconds:
                logger.debug("Selector alert suppressed by cooldown: %s", key)
                return False

            self._last_sent_by_key[key] = now
            self._global_sent_at.append(now)

        payload = {
            "text": f"[selector-healer][{severity}] {event_type} {site}/{page_type}/{field}",
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

        try:
            self._sender(webhook_url, payload)
            return True
        except Exception as exc:
            logger.warning("Selector alert dispatch failed for %s: %s", key, exc)
            return False

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with request.urlopen(req, timeout=5):
            return None


_dispatcher = AlertDispatcher()


def get_alert_dispatcher() -> AlertDispatcher:
    return _dispatcher
