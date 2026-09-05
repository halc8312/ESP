#!/usr/bin/env python3
"""Independently inspect ESP's own stack heartbeat; never fetch a marketplace.

This standard-library-only command makes one request, without redirects,
credentials, proxy inheritance, or retries. A successful process/HTTP response
alone is not scraping evidence. Missing or stale patrol observations are not
healthy. The default allow-list is the existing ESP Render web service; an
operator may supply other *exact* Render service hosts after verifying ownership.
"""

from __future__ import annotations

import argparse
from http.client import HTTPException
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_ALLOWED_HOSTS = ("esp-1-kend.onrender.com",)
REQUIRED_CHECKS = (
    "database", "redis", "worker", "scheduler", "patrol", "scrape_monitor"
)
CHECK_STATES = frozenset(
    {"ok", "unavailable", "stale", "failed", "degraded", "no_observations"}
)
MAX_RESPONSE_BYTES = 16 * 1024
_RENDER_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.onrender\.com\Z")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not follow even same-host redirects: /stack-readyz is canonical.
        return None


def _result(status: str, code: str, **safe_fields) -> dict:
    return {"status": status, "code": code, **safe_fields}


def _endpoint(base_url: str, allowed_hosts: tuple[str, ...]) -> str:
    if not allowed_hosts or any(not _RENDER_HOST.fullmatch(h) for h in allowed_hosts):
        raise ValueError("invalid host allow-list")
    if any(char.isspace() or ord(char) < 32 for char in base_url):
        raise ValueError("invalid URL")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/", "/stack-readyz")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("URL is outside the expected ESP service")
    return f"https://{parsed.hostname}/stack-readyz"


def _evaluate(payload: object, http_status: int) -> dict:
    if not isinstance(payload, dict):
        return _result("error", "invalid_response")
    checks = payload.get("checks")
    if (
        payload.get("status") not in ("ready", "not_ready")
        or not isinstance(checks, dict)
        or payload.get("runtime_role") != "web"
        or payload.get("queue_backend") != "rq"
        or not isinstance(payload.get("scheduler_enabled"), bool)
    ):
        return _result("error", "invalid_response")
    missing_checks = [name for name in REQUIRED_CHECKS if name not in checks]
    if missing_checks:
        # Older deployments can report all their known checks as green while
        # exposing no evidence about the monitor evaluator itself.
        return _result("unverified", "incomplete_checks", missing_checks=missing_checks)
    if any(checks[name] not in CHECK_STATES for name in REQUIRED_CHECKS):
        return _result("error", "invalid_response")
    safe_checks = {name: checks[name] for name in REQUIRED_CHECKS}
    failed_checks = [name for name in REQUIRED_CHECKS if checks[name] != "ok"]
    if not failed_checks:
        if http_status == 200 and payload["status"] == "ready":
            return _result("healthy", "stack_ready", checks=safe_checks)
        return _result("error", "inconsistent_response", checks=safe_checks)
    # Some deployments might incorrectly return HTTP 200 despite a bad check.
    # This is still a monitoring failure, not a healthy status-code-only ping.
    code = "no_observations" if all(
        checks[name] == "no_observations" for name in failed_checks
    ) else "stack_unhealthy"
    return _result(
        "unverified" if code == "no_observations" else "unhealthy",
        code,
        checks=safe_checks,
        failed_checks=failed_checks,
    )


def check_stack(
    base_url: str,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    timeout: float = 10,
    opener=None,
) -> dict:
    """Return only fixed status codes and sanitized health states, never URLs/body.

    ``opener`` is a test seam; production always uses the no-redirect opener.
    HTTP 503 is read because ESP intentionally uses it for unhealthy heartbeats.
    """
    if not base_url:
        return _result("unconfigured", "missing_monitor_url")
    try:
        endpoint = _endpoint(base_url, allowed_hosts)
        if not 0 < timeout <= 30:
            raise ValueError("invalid timeout")
    except (TypeError, ValueError):
        return _result("error", "invalid_configuration")

    client = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "ESP-Health-Watchdog/1.0"},
        method="GET",
    )
    response = None
    try:
        try:
            response = client.open(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        http_status = response.getcode()
        if http_status not in (200, 503):
            return _result("error", "unexpected_http_status", http_status=http_status)
        if response.headers.get_content_type() != "application/json":
            return _result("error", "invalid_response")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return _result("error", "oversized_response")
        return _evaluate(json.loads(raw), http_status)
    except (URLError, OSError, HTTPException):
        # Exception messages can contain the configured URL or remote content.
        return _result("error", "network_failure")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return _result("error", "invalid_response")
    finally:
        if response is not None:
            try:
                response.close()
            except (OSError, HTTPException):
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("ESP_MONITOR_BASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    raw_allowlist = os.environ.get("ESP_MONITOR_ALLOWED_HOSTS", "").strip()
    hosts = (
        tuple(host.strip() for host in raw_allowlist.split(","))
        if raw_allowlist
        else DEFAULT_ALLOWED_HOSTS
    )
    outcome = check_stack(args.base_url, allowed_hosts=hosts, timeout=args.timeout)
    print(json.dumps(outcome, sort_keys=True))
    if outcome["status"] == "healthy":
        return 0
    return 2 if outcome["code"] in ("missing_monitor_url", "invalid_configuration") else 1


if __name__ == "__main__":
    raise SystemExit(main())
