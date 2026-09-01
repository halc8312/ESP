"""Bounded, secret-free reachability comparisons for Record City.

The production sandbox cannot reach recordcity.jp, while a useful comparison
must run from the same Render egress as ``esp-worker``.  This module keeps the
live experiment explicit: one URL, one attempt per selected strategy, no
automatic provider fallback, and a delay between cells.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urlparse

from services.html_page_adapter import HtmlPageAdapter
from services.scrape_request import classify_target_url
from services.scrape_safety import has_no_results_evidence, validate_marketplace_url


DEFAULT_RECORDCITY_PROBE_STRATEGIES = (
    "curl-chrome120",
    "patchright-current",
    "patchright-headless-ua",
    "patchright-headful",
    "patchright-headless-proxy",
    "patchright-headful-proxy",
    "zyte",
    "scraperapi",
)

RECORDCITY_PROBE_STRATEGIES = (
    "requests-default",
    "requests-browser-headers",
    "curl-chrome120",
    "curl-chrome131",
    "curl-safari",
    "patchright-current",
    "patchright-headless-ua",
    "patchright-headful",
    "patchright-headful-tokyo",
    "patchright-headless-proxy",
    "patchright-headful-proxy",
    "zyte",
    "scraperapi",
    "template",
    "proxy",
)

_EXTERNAL_STRATEGIES = frozenset({"zyte", "scraperapi", "template", "proxy"})
_HEADFUL_STRATEGIES = frozenset(
    {
        "patchright-headful",
        "patchright-headful-tokyo",
        "patchright-headful-proxy",
    }
)
_PROXY_BROWSER_STRATEGIES = frozenset(
    {"patchright-headless-proxy", "patchright-headful-proxy"}
)
_READY_SELECTOR = {
    "detail": "script[type='application/ld+json']",
    "search": "a[href*='/catalog/']",
}
_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}
_MAX_PROBE_BODY_BYTES = 12 * 1024 * 1024
_REASON_CODE_RE = re.compile(r"\breason=([A-Z][A-Z0-9_]+)\b")


def _expected_detail_sku(url: str, kind: str) -> str | None:
    if kind != "detail":
        return None
    path = str(urlparse(str(url or "")).path or "").rstrip("/")
    return path.rsplit("/", 1)[-1] if path else None


def _safe_observed_text(value, limit: int) -> str:
    text = "".join(
        character
        for character in str(value or "")
        if character >= " " and character not in {"\x7f"}
    )
    return text[: max(0, int(limit))]


def _normalize_headers(headers) -> dict[str, str]:
    try:
        items = dict(headers or {}).items()
    except Exception:
        return {}
    return {
        _safe_observed_text(key, 120).strip().lower(): _safe_observed_text(
            value,
            240,
        ).strip()
        for key, value in items
    }


def _looks_like_challenge(html: str) -> bool:
    normalized = str(html or "").lower()
    return (
        "window.gokuprops" in normalized
        or (
            "awswafintegration" in normalized
            and ("challenge.js" in normalized or "token.awswaf.com" in normalized)
        )
    )


def _looks_like_captcha(html: str) -> bool:
    normalized = str(html or "").lower()
    return "awswafcaptcha" in normalized or (
        "token.awswaf.com" in normalized and "captcha.js" in normalized
    )


def _looks_like_block(html: str) -> bool:
    normalized = " ".join(str(html or "").lower().split())
    return "request blocked" in normalized or "access denied" in normalized


def _has_product_json_ld(html: str, expected_sku: str | None = None) -> bool:
    # Keep Product detection identical to the browser diagnostic without
    # exposing the response body in the result.
    from services.recordcity_browser_fetch import _contains_product_json_ld

    return _contains_product_json_ld(html, expected_sku=expected_sku)


def _cookie_name_present(cookies, name: str) -> bool:
    try:
        getter = getattr(cookies, "get_dict", None)
        if callable(getter):
            return name in dict(getter() or {})
        return any(str(getattr(cookie, "name", "")) == name for cookie in cookies)
    except Exception:
        return False


def _base_result(strategy: str) -> dict:
    return {
        "strategy": strategy,
        "attempted": False,
        "transport_status": None,
        "target_status": None,
        "header_source": "",
        "waf_action": "",
        "server": "",
        "x_cache": "",
        "cloudfront_request_ids": [],
        "challenge": False,
        "captcha": False,
        "blocked_marker": False,
        "aws_waf_token": False,
        "ready_dom": False,
        "product_json_ld": False,
        "body_bytes": 0,
        "body_sha256": "",
        "elapsed_ms": 0,
        "outcome": "skipped",
        "reason": "",
        "error_type": "",
    }


def _result_from_html(
    strategy: str,
    *,
    html: str,
    kind: str,
    target_status: int | None,
    transport_status: int | None,
    headers,
    header_source: str,
    token_present: bool,
    elapsed_ms: int,
    expected_sku: str | None = None,
) -> dict:
    result = _base_result(strategy)
    normalized_headers = _normalize_headers(headers)
    target_headers = header_source in {"target", "browser"}
    waf_action = (
        str(normalized_headers.get("x-amzn-waf-action") or "").lower()
        if target_headers
        else ""
    )
    cf_id = (
        _safe_observed_text(normalized_headers.get("x-amz-cf-id"), 200)
        if target_headers
        else ""
    )
    body = str(html or "")
    body_bytes = body.encode("utf-8", errors="ignore")
    page = HtmlPageAdapter(body)
    challenge = waf_action == "challenge" or _looks_like_challenge(body)
    captcha = (
        waf_action == "captcha"
        or target_status == 405
        or _looks_like_captcha(body)
    )
    ready_dom = bool(page.css(_READY_SELECTOR[kind]))
    if kind == "search" and not ready_dom:
        ready_dom = has_no_results_evidence(page.get_text(), "recordcity")
    product_json_ld = _has_product_json_ld(body, expected_sku=expected_sku)
    blocked_marker = _looks_like_block(body)

    if ready_dom and (kind == "search" or product_json_ld):
        outcome = "success"
    elif captcha:
        outcome = "captcha"
    elif target_status == 403 or blocked_marker:
        outcome = "blocked_403"
    elif challenge or target_status == 202:
        outcome = "challenge"
    else:
        outcome = "target_dom_missing"

    result.update(
        {
            "attempted": True,
            "transport_status": transport_status,
            "target_status": target_status,
            "header_source": header_source,
            "waf_action": waf_action,
            "server": _safe_observed_text(normalized_headers.get("server"), 40),
            "x_cache": _safe_observed_text(normalized_headers.get("x-cache"), 80),
            "cloudfront_request_ids": [cf_id] if cf_id else [],
            "challenge": challenge,
            "captcha": captcha,
            "blocked_marker": blocked_marker,
            "aws_waf_token": bool(token_present),
            "ready_dom": ready_dom,
            "product_json_ld": product_json_ld,
            "body_bytes": len(body_bytes),
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
            "elapsed_ms": elapsed_ms,
            "outcome": outcome,
        }
    )
    return result


def _probe_requests(
    url: str,
    *,
    kind: str,
    strategy: str,
    timeout_seconds: float,
) -> dict:
    import requests

    started = time.monotonic()
    headers = _BROWSER_HEADERS if strategy == "requests-browser-headers" else None
    response = requests.get(
        url,
        headers=headers,
        timeout=timeout_seconds,
        allow_redirects=False,
        stream=True,
    )
    try:
        content_length = str(response.headers.get("Content-Length") or "")
        if content_length and int(content_length) > _MAX_PROBE_BODY_BYTES:
            raise ValueError("recordcity_probe_body_too_large")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if len(body) + len(chunk) > _MAX_PROBE_BODY_BYTES:
                raise ValueError("recordcity_probe_body_too_large")
            body.extend(chunk)
        encoding = str(getattr(response, "encoding", "") or "utf-8")
        html = bytes(body).decode(encoding, errors="ignore")
        return _result_from_html(
            strategy,
            html=html,
            kind=kind,
            target_status=int(response.status_code),
            transport_status=None,
            headers=response.headers,
            header_source="target",
            token_present=_cookie_name_present(response.cookies, "aws-waf-token"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            expected_sku=_expected_detail_sku(url, kind),
        )
    finally:
        response.close()


def _probe_curl(
    url: str,
    *,
    kind: str,
    strategy: str,
    timeout_seconds: float,
) -> dict:
    from curl_cffi import requests as curl_requests

    impersonate = {
        "curl-chrome120": "chrome120",
        "curl-chrome131": "chrome131",
        "curl-safari": "safari",
    }[strategy]
    started = time.monotonic()
    body = bytearray()

    def _collect(chunk) -> None:
        value = bytes(chunk or b"")
        if len(body) + len(value) > _MAX_PROBE_BODY_BYTES:
            raise ValueError("recordcity_probe_body_too_large")
        body.extend(value)

    response = curl_requests.get(
        url,
        impersonate=impersonate,
        headers={"Accept-Language": _BROWSER_HEADERS["Accept-Language"]},
        timeout=timeout_seconds,
        allow_redirects=False,
        content_callback=_collect,
    )
    encoding = str(getattr(response, "encoding", "") or "utf-8")
    html = bytes(body).decode(encoding, errors="ignore")
    return _result_from_html(
        strategy,
        html=html,
        kind=kind,
        target_status=int(response.status_code),
        transport_status=None,
        headers=response.headers,
        header_source="target",
        token_present=_cookie_name_present(response.cookies, "aws-waf-token"),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        expected_sku=_expected_detail_sku(url, kind),
    )


def _probe_browser(
    url: str,
    *,
    kind: str,
    strategy: str,
    timeout_seconds: float,
) -> dict:
    from services.recordcity_browser_fetch import probe_recordcity_browser_once_sync

    result = probe_recordcity_browser_once_sync(
        url,
        profile=strategy,
        timeout=max(1, int(timeout_seconds * 1000)),
        wait_selector_timeout=min(20000, max(1, int(timeout_seconds * 1000))),
    )
    result.setdefault("outcome", "")
    if result.get("ready_dom") and (
        kind == "search" or result.get("product_json_ld")
    ):
        result["outcome"] = "success"
    elif result.get("captcha"):
        result["outcome"] = "captcha"
    elif result.get("target_status") == 403:
        result["outcome"] = "blocked_403"
    elif result.get("challenge"):
        result["outcome"] = "challenge"
    elif result.get("navigation_error"):
        result["outcome"] = "navigation_error"
    else:
        result["outcome"] = "target_dom_missing"
    result.setdefault("reason", str(result.get("failure_reason") or ""))
    result.setdefault("error_type", str(result.get("navigation_error") or ""))
    return result


def _probe_external(
    url: str,
    *,
    kind: str,
    strategy: str,
    timeout_seconds: float,
) -> dict:
    from services.recordcity_external_fetch import fetch_recordcity_external

    started = time.monotonic()
    response = fetch_recordcity_external(
        url,
        timeout=max(1, int(timeout_seconds)),
        provider=strategy,
    )
    if response is None:
        result = _base_result(strategy)
        result["reason"] = "provider_not_configured"
        return result

    status_source = str(
        getattr(response, "status_source", "unknown") or "unknown"
    )
    target_status = None
    if status_source in {"target", "target_metadata"}:
        target_status = getattr(response, "target_status", None)
        if target_status is None:
            target_status = getattr(response, "status_code", None)
    transport_status = getattr(response, "transport_status", None)
    target_headers = getattr(response, "target_headers", {}) or {}
    token_present = bool(
        getattr(
            response,
            "aws_waf_token",
            getattr(response, "waf_token_present", False),
        )
    )
    result = _result_from_html(
        strategy,
        html=str(getattr(response, "text", "") or ""),
        kind=kind,
        target_status=int(target_status) if target_status is not None else None,
        transport_status=(
            int(transport_status) if transport_status is not None else None
        ),
        headers=target_headers,
        header_source=str(getattr(response, "header_source", "unknown") or "unknown"),
        token_present=token_present,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        expected_sku=_expected_detail_sku(url, kind),
    )
    result["provider"] = strategy
    result["status_source"] = status_source
    if (
        status_source == "provider"
        and isinstance(transport_status, int)
        and not 200 <= transport_status < 300
    ):
        result["ready_dom"] = False
        result["product_json_ld"] = False
        result["outcome"] = "external_provider_error"
        result["reason"] = "RC_EXTERNAL_PROVIDER_HTTP_ERROR"
    elif status_source == "provider" and (
        result.get("challenge")
        or result.get("captcha")
        or result.get("blocked_marker")
    ):
        # Without target response metadata the HTML may be a block page from
        # the provider itself. Preserve the observed markers, but do not label
        # their origin as RecordCity or use them for causal conclusions.
        result["outcome"] = "external_block_source_ambiguous"
        result["reason"] = "RC_EXTERNAL_BLOCK_SOURCE_AMBIGUOUS"
    return result


def _skip(strategy: str, reason: str) -> dict:
    result = _base_result(strategy)
    result["reason"] = reason
    return result


def _dedupe_strategies(strategies: Iterable[str]) -> list[str]:
    selected: list[str] = []
    for value in strategies:
        strategy = str(value or "").strip().lower()
        if strategy not in RECORDCITY_PROBE_STRATEGIES:
            supported = ", ".join(RECORDCITY_PROBE_STRATEGIES)
            raise ValueError(f"Unknown Record City probe strategy: {strategy} ({supported})")
        if strategy not in selected:
            selected.append(strategy)
    if not selected:
        selected = list(DEFAULT_RECORDCITY_PROBE_STRATEGIES)
    return selected


def _assess_results(rows: list[dict]) -> dict:
    by_strategy = {str(row.get("strategy") or ""): row for row in rows}

    def _attempted(name: str) -> bool:
        return bool(by_strategy.get(name, {}).get("attempted"))

    def _success(name: str) -> bool:
        return by_strategy.get(name, {}).get("outcome") == "success"

    def _waf_failure(name: str) -> bool:
        row = by_strategy.get(name, {})
        actions = {
            str(row.get("waf_action") or "").strip().lower(),
            *(
                str(action or "").strip().lower()
                for action in (row.get("waf_actions_seen") or [])
            ),
        }
        waf_evidence = bool(
            row.get("challenge")
            or row.get("captcha")
            or row.get("blocked_marker")
            or actions.intersection({"challenge", "captcha"})
        )
        return bool(
            row.get("attempted")
            and row.get("outcome") in {"challenge", "captcha", "blocked_403"}
            and isinstance(row.get("target_status"), int)
            and waf_evidence
        )

    current = "patchright-current"
    headless_ua = "patchright-headless-ua"
    headful = "patchright-headful"
    headless_proxy = "patchright-headless-proxy"
    headful_proxy = "patchright-headful-proxy"

    if _waf_failure(current) and _success(headless_ua):
        return {
            "code": "headless_user_agent_factor_supported",
            "summary": "同じheadlessで通常Chrome UAだけ成功したため、HeadlessChrome UA要因が強い。",
        }
    if _waf_failure(current) and _success(headful):
        return {
            "code": "browser_mode_factor_supported",
            "summary": "Render直通でheadfulだけ成功したため、headless/automation要因が強い。",
        }
    if all(_attempted(name) for name in (current, headful, headless_proxy, headful_proxy)):
        direct_failed = _waf_failure(current) and _waf_failure(headful)
        proxy_succeeded = _success(headless_proxy) and _success(headful_proxy)
        if direct_failed and proxy_succeeded:
            return {
                "code": "render_egress_factor_supported",
                "summary": "両browser modeがproxy経由だけ成功したため、Render egress/IP要因が強い。",
            }
        if (
            _waf_failure(current)
            and _waf_failure(headful)
            and _waf_failure(headless_proxy)
            and _success(headful_proxy)
        ):
            return {
                "code": "browser_and_egress_interaction_supported",
                "summary": "headful+proxyだけ成功したため、browser modeとegressの両方または相互作用が疑われる。",
            }
    external_success = any(
        row.get("strategy") in _EXTERNAL_STRATEGIES
        and row.get("outcome") == "success"
        for row in rows
    )
    if external_success:
        return {
            "code": "external_path_success_cause_unresolved",
            "summary": "外部経路では取得できたが、IPとbrowser fingerprintの寄与は分離できない。",
        }
    return {
        "code": "inconclusive",
        "summary": "現行結果だけではheadlessと送信元IPの寄与を判別できない。",
    }


def run_recordcity_probe(
    url: str,
    *,
    strategies: Iterable[str] = (),
    timeout_seconds: float = 60.0,
    delay_seconds: float = 5.0,
    allow_external: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """Execute each selected strategy at most once against one validated URL."""
    request_kind, site = classify_target_url(url)
    if site != "recordcity" or request_kind not in {"item", "search"}:
        raise ValueError("Record Cityの商品または一覧URLを指定してください。")
    kind = "detail" if request_kind == "item" else "search"
    normalized_url = validate_marketplace_url(url, "recordcity", kind=kind)
    selected = _dedupe_strategies(strategies)

    configured_external: set[str] = set()
    try:
        from services.recordcity_external_fetch import (
            configured_recordcity_external_providers,
        )

        configured_external = set(configured_recordcity_external_providers())
    except ImportError:
        configured_external = set()

    rows: list[dict] = []
    for index, strategy in enumerate(selected):
        cell_started = time.monotonic()
        if strategy in _HEADFUL_STRATEGIES and not os.environ.get("DISPLAY"):
            row = _skip(strategy, "display_not_configured_use_xvfb_run")
        elif (
            strategy in _PROXY_BROWSER_STRATEGIES
            and not str(os.environ.get("RECORDCITY_PROXY_URL") or "").strip()
        ):
            row = _skip(strategy, "provider_not_configured")
        elif strategy in _PROXY_BROWSER_STRATEGIES and not allow_external:
            row = _skip(strategy, "external_requires_allow_flag")
        elif strategy in _EXTERNAL_STRATEGIES and strategy not in configured_external:
            row = _skip(strategy, "provider_not_configured")
        elif strategy in _EXTERNAL_STRATEGIES and not allow_external:
            row = _skip(strategy, "external_requires_allow_flag")
        else:
            try:
                if strategy.startswith("requests-"):
                    row = _probe_requests(
                        normalized_url,
                        kind=kind,
                        strategy=strategy,
                        timeout_seconds=timeout_seconds,
                    )
                elif strategy.startswith("curl-"):
                    row = _probe_curl(
                        normalized_url,
                        kind=kind,
                        strategy=strategy,
                        timeout_seconds=timeout_seconds,
                    )
                elif strategy.startswith("patchright-"):
                    row = _probe_browser(
                        normalized_url,
                        kind=kind,
                        strategy=strategy,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    row = _probe_external(
                        normalized_url,
                        kind=kind,
                        strategy=strategy,
                        timeout_seconds=timeout_seconds,
                    )
            except Exception as exc:
                row = _base_result(strategy)
                reason_match = _REASON_CODE_RE.search(str(exc or ""))
                status_code = getattr(exc, "status_code", None)
                row.update(
                    {
                        "attempted": True,
                        "elapsed_ms": int(
                            (time.monotonic() - cell_started) * 1000
                        ),
                        "outcome": "error",
                        "transport_status": (
                            int(status_code)
                            if strategy in _EXTERNAL_STRATEGIES
                            and isinstance(status_code, int)
                            else None
                        ),
                        "reason": (
                            reason_match.group(1)
                            if reason_match
                            else "strategy_exception"
                        ),
                        "error_type": type(exc).__name__,
                    }
                )
        rows.append(row)
        if row.get("attempted") and index < len(selected) - 1:
            sleep_fn(max(0.0, float(delay_seconds)))

    return {
        "probe_id": secrets.token_hex(4),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site": "recordcity",
        "kind": kind,
        "url": normalized_url,
        "delay_seconds": float(delay_seconds),
        "timeout_seconds": float(timeout_seconds),
        "allow_external": bool(allow_external),
        "strategies": selected,
        "results": rows,
        "assessment": _assess_results(rows),
    }


def format_recordcity_probe_table(snapshot: dict) -> str:
    columns = (
        ("strategy", 27),
        ("status", 6),
        ("action", 9),
        ("challenge", 9),
        ("captcha", 7),
        ("token", 5),
        ("product", 7),
        ("ms", 8),
        ("outcome", 22),
    )

    def _cell(value, width: int) -> str:
        text = str(value)
        if len(text) > width:
            text = text[: max(0, width - 1)] + "…"
        return text.ljust(width)

    header = " | ".join(_cell(name, width) for name, width in columns)
    divider = "-+-".join("-" * width for _, width in columns)
    lines = [header, divider]
    for row in snapshot.get("results") or []:
        values = {
            "strategy": row.get("strategy", ""),
            "status": row.get("target_status") if row.get("attempted") else "-",
            "action": row.get("waf_action") or "-",
            "challenge": bool(row.get("challenge")),
            "captcha": bool(row.get("captcha")),
            "token": bool(row.get("aws_waf_token")),
            "product": bool(row.get("product_json_ld")),
            "ms": row.get("elapsed_ms", 0),
            "outcome": (
                row.get("outcome")
                if row.get("attempted")
                else row.get("reason") or "skipped"
            ),
        }
        lines.append(
            " | ".join(_cell(values[name], width) for name, width in columns)
        )
    return "\n".join(lines)
