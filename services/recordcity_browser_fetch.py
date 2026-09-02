"""Record City browser fetches with AWS WAF-aware diagnostics.

Record City is the only supported marketplace that currently returns an AWS
WAF Challenge interstitial.  Keep the patched browser profile, token reuse,
and diagnostic detail here so Mercari, Surugaya, SNKRDUNK, and the generic
dynamic fetch path retain their existing behaviour.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlparse

from services.browser_pool import run_browser_page_task
from services.html_page_adapter import HtmlPageAdapter
from services.scrape_request import classify_target_url
from services.scrape_safety import (
    ScrapeBlockedError,
    ScrapeHttpError,
    ScrapeSelectorDriftError,
    has_no_results_evidence,
    install_navigation_guard,
    raise_for_blocked_navigation,
    validate_fetch_response,
    validate_marketplace_url,
)
from services.scraping_client import run_coro_sync


logger = logging.getLogger(__name__)

_SITE = "recordcity"
_WAF_COOKIE_NAME = "aws-waf-token"
_MAX_EVENTS = 12
_MAX_DIAGNOSTIC_BODY_BYTES = 50 * 1024 * 1024
_MAX_DIAGNOSTIC_HTML_CHARS = 256 * 1024
_MAX_DIAGNOSTIC_TITLE_SIZE = 4096
_MAX_DIAGNOSTIC_DOM_COUNT = 10000

# Patchright removes Playwright's automation flags itself.  Passing ESP's
# ordinary ``--disable-extensions`` flag would put one of those fingerprints
# back, so an explicit empty list is intentional.
_LAUNCH_ARGS: list[str] = []
_CONTEXT_OPTIONS = {"locale": "ja-JP"}
_CHROMIUM_145_LINUX_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
# A same-runtime A/B probe on Render changed only this value and moved the
# Record City response from token-then-403 to validated Product/listing HTML.
# Keep it site-scoped; the generic browser pool and other marketplaces must
# retain their browser-generated user agents.
_PRODUCTION_CONTEXT_OVERRIDES = {
    "user_agent": _CHROMIUM_145_LINUX_UA,
}
_HEADFUL_CONTEXT_OVERRIDES = {
    "no_viewport": True,
}
_PRODUCTION_PROFILE_ENV = "RECORDCITY_BROWSER_PROFILE"
_PRODUCTION_BROWSER_PROFILES: dict[str, dict] = {
    "headless": {
        "headless": True,
        "context_options": _PRODUCTION_CONTEXT_OVERRIDES,
        "runtime_site": _SITE,
        "profile_label": "patchright/chromium/headless",
    },
    "headful": {
        "headless": False,
        "context_options": _HEADFUL_CONTEXT_OVERRIDES,
        # A browser runtime is keyed by site.  A separate key prevents a
        # previously-started headless runtime from being silently reused.
        "runtime_site": "recordcity_headful",
        "profile_label": "patchright/chromium/headful",
    },
}

_WAF_COOKIE_LOCK = threading.RLock()
_WAF_COOKIES: list[dict] = []
_FETCH_LOCK = threading.Lock()


def _host(url: str) -> str:
    try:
        return str(urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _is_recordcity_host(url: str) -> bool:
    return _host(url) in {"recordcity.jp", "www.recordcity.jp"}


def _is_waf_token_host(url: str) -> bool:
    host = _host(url)
    return host == "token.awswaf.com" or host.endswith(".token.awswaf.com")


def _cookie_is_current(cookie: dict, now: float | None = None) -> bool:
    expires = cookie.get("expires", -1)
    try:
        expires = float(expires)
    except (TypeError, ValueError):
        return False
    return expires <= 0 or expires > (time.time() if now is None else now)


def _is_recordcity_waf_cookie(cookie: dict) -> bool:
    if str(cookie.get("name") or "") != _WAF_COOKIE_NAME:
        return False
    domain = str(cookie.get("domain") or "").lower().lstrip(".").rstrip(".")
    return domain == "recordcity.jp" or domain.endswith(".recordcity.jp")


def _cached_waf_cookies() -> list[dict]:
    """Return an in-memory copy; token values are never logged or persisted."""
    global _WAF_COOKIES
    with _WAF_COOKIE_LOCK:
        current = [
            copy.deepcopy(cookie)
            for cookie in _WAF_COOKIES
            if _is_recordcity_waf_cookie(cookie) and _cookie_is_current(cookie)
        ]
        _WAF_COOKIES = copy.deepcopy(current)
    return current


def _current_waf_cookies(cookies) -> list[dict]:
    return [
        copy.deepcopy(cookie)
        for cookie in (cookies or ())
        if isinstance(cookie, dict)
        and _is_recordcity_waf_cookie(cookie)
        and _cookie_is_current(cookie)
    ]


def _remember_waf_cookies(cookies) -> bool:
    global _WAF_COOKIES
    current = _current_waf_cookies(cookies)
    with _WAF_COOKIE_LOCK:
        _WAF_COOKIES = current
    return bool(current)


def _discard_cached_waf_cookies() -> None:
    global _WAF_COOKIES
    with _WAF_COOKIE_LOCK:
        _WAF_COOKIES = []


def _context_options(cookies: list[dict], overrides: dict | None = None) -> dict:
    options = copy.deepcopy(_CONTEXT_OPTIONS)
    if overrides:
        options.update(copy.deepcopy(overrides))
    if cookies:
        options["storage_state"] = {"cookies": copy.deepcopy(cookies), "origins": []}
    return options


def _append_bounded(values: list, value) -> None:
    if values and values[-1] == value:
        return
    values.append(value)
    if len(values) > _MAX_EVENTS:
        del values[0 : len(values) - _MAX_EVENTS]


@dataclass
class _WafProbe:
    probe_id: str = field(default_factory=lambda: secrets.token_hex(4))
    attempt: int = 1
    profile: str = "patchright/chromium/headless"
    main_responses: list[dict] = field(default_factory=list)
    last_main_response: dict | None = None
    waf_responses: list[dict] = field(default_factory=list)
    cloudfront_request_ids: list[str] = field(default_factory=list)
    page_error_count: int = 0
    waf_request_failures: list[dict] = field(default_factory=list)
    ready: bool = False
    token_before: bool = False
    token_after: bool = False
    token_cookie_metadata: list[dict] = field(default_factory=list)
    webdriver_true: bool | None = None
    headless_user_agent: bool | None = None
    user_agent: str = ""
    language: str = ""
    timezone: str = ""
    platform: str = ""
    screen: str = ""
    webgl_vendor: str = ""
    webgl_renderer: str = ""

    @property
    def final_status(self) -> int | None:
        if not self.last_main_response:
            return None
        return self.last_main_response.get("status")

    @property
    def final_action(self) -> str:
        return str((self.last_main_response or {}).get("action") or "")


@dataclass
class _AttemptResult:
    html: str
    url: str
    status: int
    probe: _WafProbe
    waf_cookies: list[dict] = field(repr=False)
    navigation_error: Exception | None = None


async def _all_headers(response) -> dict[str, str]:
    try:
        headers = await response.all_headers()
    except Exception:
        headers = getattr(response, "headers", {}) or {}
    return {str(key).lower(): str(value) for key, value in dict(headers or {}).items()}


def _is_document_response(response) -> bool:
    request = getattr(response, "request", None)
    resource_type = str(getattr(request, "resource_type", "") or "").lower()
    if resource_type:
        return resource_type == "document"
    checker = getattr(request, "is_navigation_request", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def _is_top_level_document_response(response) -> bool:
    if not _is_document_response(response):
        return False
    request = getattr(response, "request", None)
    frame = getattr(request, "frame", None)
    if frame is None:
        # Lightweight adapters and some failed navigation responses do not
        # expose a Frame. The explicit goto response is also recorded with
        # force_main=True, so this fallback does not weaken URL validation.
        return True
    try:
        return getattr(frame, "parent_frame", None) is None
    except Exception:
        return False


def _should_capture_response(response) -> bool:
    response_url = str(getattr(response, "url", "") or "")
    return _is_waf_token_host(response_url) or (
        _is_recordcity_host(response_url)
        and _is_top_level_document_response(response)
    )


async def _record_response(probe: _WafProbe, response, *, force_main: bool = False) -> None:
    if response is None:
        return
    response_url = str(getattr(response, "url", "") or "")
    status = int(getattr(response, "status", 0) or 0)
    if _is_waf_token_host(response_url):
        _append_bounded(
            probe.waf_responses,
            {
                "status": status or None,
                "resource_type": str(
                    getattr(getattr(response, "request", None), "resource_type", "") or ""
                ).lower(),
            },
        )
        return
    if not _is_recordcity_host(response_url) or (
        not force_main and not _is_top_level_document_response(response)
    ):
        return

    headers = await _all_headers(response)
    action = str(headers.get("x-amzn-waf-action") or "").strip().lower()
    entry = {
        "status": status or None,
        "action": action,
        "server": str(headers.get("server") or "").strip()[:40],
        "x_cache": str(headers.get("x-cache") or "").strip()[:80],
    }
    _append_bounded(probe.main_responses, entry)
    probe.last_main_response = entry
    request_id = str(headers.get("x-amz-cf-id") or "").strip()
    if request_id:
        _append_bounded(probe.cloudfront_request_ids, request_id[:200])


def _record_request_failure(probe: _WafProbe, request) -> None:
    if _is_waf_token_host(str(getattr(request, "url", "") or "")):
        failure = getattr(request, "failure", "")
        if callable(failure):
            try:
                failure = failure()
            except Exception:
                failure = ""
        _append_bounded(
            probe.waf_request_failures,
            {
                "resource_type": str(getattr(request, "resource_type", "") or "")[:40],
                "failure": str(failure or "")[:160],
            },
        )


async def _capture_browser_signals(probe: _WafProbe, page) -> None:
    try:
        signals = await page.evaluate(
            """() => {
                let webglVendor = '';
                let webglRenderer = '';
                try {
                    const gl = document.createElement('canvas').getContext('webgl');
                    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
                    if (gl && ext) {
                        webglVendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || '';
                        webglRenderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '';
                    }
                } catch (_error) {}
                return {
                    webdriverTrue: navigator.webdriver === true,
                    headlessUserAgent: /HeadlessChrome/i.test(navigator.userAgent || ''),
                    userAgent: navigator.userAgent || '',
                    language: navigator.language || '',
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                    platform: navigator.platform || '',
                    screen: `${screen.width || 0}x${screen.height || 0}@${devicePixelRatio || 1}`,
                    webglVendor,
                    webglRenderer
                };
            }"""
        )
    except Exception:
        return
    if isinstance(signals, dict):
        probe.webdriver_true = bool(signals.get("webdriverTrue"))
        probe.headless_user_agent = bool(signals.get("headlessUserAgent"))
        probe.user_agent = str(signals.get("userAgent") or "")[:240]
        probe.language = str(signals.get("language") or "")[:40]
        probe.timezone = str(signals.get("timezone") or "")[:80]
        probe.platform = str(signals.get("platform") or "")[:80]
        probe.screen = str(signals.get("screen") or "")[:80]
        probe.webgl_vendor = str(signals.get("webglVendor") or "")[:160]
        probe.webgl_renderer = str(signals.get("webglRenderer") or "")[:240]


def _looks_like_waf_challenge(html: str) -> bool:
    normalized = str(html or "").lower()
    return (
        "window.gokuprops" in normalized
        or (
            "awswafintegration" in normalized
            and ("challenge.js" in normalized or "token.awswaf.com" in normalized)
        )
    )


def _looks_like_waf_captcha(html: str) -> bool:
    normalized = str(html or "").lower()
    return "awswafcaptcha" in normalized or (
        "token.awswaf.com" in normalized and "captcha.js" in normalized
    )


def _has_ready_evidence(html: str, wait_selector: str, *, kind: str) -> bool:
    page = HtmlPageAdapter(str(html or ""))
    if page.css(wait_selector):
        return True
    return kind == "search" and has_no_results_evidence(
        page.get_text(),
        _SITE,
    )


def _contains_product_json_ld(html: str, expected_sku: str | None = None) -> bool:
    """Return whether rendered HTML contains a parseable Product JSON-LD node."""

    def _contains_product(value) -> bool:
        pending = value if isinstance(value, list) else [value]
        processed_nodes = 0
        while pending and processed_nodes < 256:
            entry = pending.pop(0)
            processed_nodes += 1
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("@type")
            types = entry_type if isinstance(entry_type, list) else [entry_type]
            if any(str(item).lower() == "product" for item in types if item):
                if expected_sku is None:
                    return True
                if str(entry.get("sku") or "").strip() == str(expected_sku):
                    return True
            graph = entry.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)
        return False

    page = HtmlPageAdapter(str(html or ""))
    for script in page.css("script[type='application/ld+json']")[:32]:
        raw = str(
            getattr(script, "raw_text", "")
            or getattr(script, "text", "")
            or ""
        ).strip()
        if not raw:
            continue
        if len(raw.encode("utf-8", errors="ignore")) > 1024 * 1024:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if _contains_product(value):
            return True
    return False


def _classify_waf_failure(probe: _WafProbe, html: str) -> tuple[str, str] | None:
    """Return a stable reason code and operator-facing Japanese explanation."""
    status = probe.final_status
    final_action = probe.final_action
    captcha_html = _looks_like_waf_captcha(html)
    captcha_seen = final_action == "captcha" or captcha_html

    # CAPTCHA is an explicit human-verification requirement.  Never turn the
    # current CAPTCHA response into a success just because the document also
    # contains Product markup (for example, markup retained below an overlay).
    # This only considers the final action/current DOM; an earlier Challenge
    # followed by a final 200 Product document remains a valid success.
    if captcha_seen:
        return (
            "RC_WAF_CAPTCHA_REQUIRED",
            "AWS WAFがCAPTCHAを要求したため、自動取得を続行できませんでした",
        )

    # Current product/listing DOM is stronger evidence than a bounded history
    # containing an earlier 202 Challenge response.
    if probe.ready:
        return None

    challenge_html = _looks_like_waf_challenge(html)
    blocked_html = "request blocked" in str(html or "").lower()
    challenge_unresolved = (
        final_action == "challenge"
        or challenge_html
    )
    actions_seen = {
        str(entry.get("action") or "").strip().lower()
        for entry in probe.main_responses
    }
    waf_evidence = bool(
        challenge_unresolved
        or captcha_seen
        or blocked_html
        or actions_seen.intersection({"challenge", "captcha"})
        or probe.waf_responses
        or probe.waf_request_failures
        or probe.token_after
    )

    if status == 403 and waf_evidence:
        return (
            "RC_WAF_BLOCK_403",
            "AWS WAFにアクセスを拒否されました。Challenge後の別ルール、ブラウザ判定、または送信元IP判定の可能性があります",
        )
    if challenge_unresolved and probe.token_after:
        return (
            "RC_WAF_TOKEN_PRESENT_CHALLENGE_CONTINUED",
            "AWS WAFトークンは存在しますが、Challengeが継続しました",
        )
    if challenge_unresolved and (
        probe.waf_request_failures
        or any((entry.get("status") or 0) >= 400 for entry in probe.waf_responses)
    ):
        return (
            "RC_WAF_CHALLENGE_SCRIPT_FAILED",
            "AWS WAF Challenge用スクリプトの通信に失敗し、トークンを取得できませんでした",
        )
    if challenge_unresolved:
        return (
            "RC_WAF_CHALLENGE_NO_TOKEN",
            "AWS WAF Challengeを完了できず、トークンが発行されませんでした",
        )
    return None


def _raise_waf_failure(probe: _WafProbe, reason_code: str, explanation: str) -> None:
    status = probe.final_status
    logger.warning(
        "Record City WAF probe failed: probe=%s attempt=%d "
        "profile=%s reason=%s main=%s waf=%s "
        "token_before=%s token_after=%s webdriver_true=%s headless_ua=%s "
        "user_agent=%s language=%s timezone=%s token_cookie_metadata=%s "
        "waf_request_failures=%s page_errors=%d "
        "cloudfront_request_ids=%s",
        probe.probe_id,
        probe.attempt,
        probe.profile,
        reason_code,
        probe.main_responses,
        probe.waf_responses,
        probe.token_before,
        probe.token_after,
        probe.webdriver_true,
        probe.headless_user_agent,
        probe.user_agent,
        probe.language,
        probe.timezone,
        probe.token_cookie_metadata,
        probe.waf_request_failures,
        probe.page_error_count,
        probe.cloudfront_request_ids,
    )
    status_text = f"HTTP {status}" if status else "HTTP status不明"
    raise ScrapeBlockedError(
        f"レコードシティの取得をAWS WAFに阻止されました: {explanation}"
        f"（reason={reason_code}, probe={probe.probe_id}, {status_text}）。"
        "正確な発火ルールはレコードシティ側のWAFログでの照合が必要です。",
        status_code=status,
    )


def _log_waf_success(probe: _WafProbe) -> None:
    logger.info(
        "Record City WAF fetch passed: probe=%s attempt=%d "
        "profile=%s main=%s waf=%s "
        "token_before=%s token_after=%s webdriver_true=%s headless_ua=%s "
        "user_agent=%s language=%s timezone=%s token_cookie_metadata=%s "
        "waf_request_failures=%s page_errors=%d cloudfront_request_ids=%s",
        probe.probe_id,
        probe.attempt,
        probe.profile,
        probe.main_responses,
        probe.waf_responses,
        probe.token_before,
        probe.token_after,
        probe.webdriver_true,
        probe.headless_user_agent,
        probe.user_agent,
        probe.language,
        probe.timezone,
        probe.token_cookie_metadata,
        probe.waf_request_failures,
        probe.page_error_count,
        probe.cloudfront_request_ids,
    )


def _fixed_class(value: str, classes: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return "empty"
    for class_name, markers in classes:
        if any(marker in normalized for marker in markers):
            return class_name
    return "other"


def _status_value(value) -> int | None:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _safe_cloudfront_request_ids(values: list[str]) -> list[str]:
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+/=-"
    )
    result = []
    for raw_value in list(values or ())[-3:]:
        value = str(raw_value or "")[:128]
        if value and all(character in allowed for character in value):
            result.append(value)
    return result


def _url_diagnostic(final_url: str, requested_url: str) -> dict:
    invalid = {
        "host": "invalid",
        "path": "invalid",
        "query_present": False,
        "query_parse": "invalid",
        "keyword_present": None,
        "fragment_present": False,
        "exact_url_match": False,
    }
    try:
        parsed = urlparse(str(final_url or ""))
        host = str(parsed.hostname or "").lower().rstrip(".")
        parts = [part for part in str(parsed.path or "").split("/") if part]
    except (TypeError, ValueError):
        return invalid

    if not parsed.query:
        query_pairs, query_parse = [], "empty"
    else:
        try:
            query_pairs = parse_qsl(
                str(parsed.query),
                keep_blank_values=True,
                max_num_fields=128,
            )
            query_parse = "ok"
        except ValueError:
            query_pairs, query_parse = [], "capped"

    has_locale_prefix = bool(
        parts
        and len(parts[0]) == 2
        and parts[0].isascii()
        and parts[0].isalpha()
        and parts[0].islower()
    )
    catalog_parts = parts[1:] if has_locale_prefix else parts
    if catalog_parts == ["catalog"]:
        path_class = "catalog_search"
    elif (
        len(catalog_parts) == 2
        and catalog_parts[0] == "catalog"
        and catalog_parts[1].isdigit()
    ):
        path_class = "catalog_detail"
    else:
        path_class = "other"

    return {
        "host": (
            "www"
            if host == "www.recordcity.jp"
            else "apex" if host == "recordcity.jp" else "other"
        ),
        "path": path_class,
        "query_present": bool(parsed.query),
        "query_parse": query_parse,
        "keyword_present": (
            any(
                str(key or "").strip().lower() == "keyword"
                for key, _value in query_pairs
            )
            if query_parse in {"empty", "ok"}
            else None
        ),
        "fragment_present": bool(parsed.fragment),
        "exact_url_match": str(final_url or "") == str(requested_url or ""),
    }


def _chrome_major(user_agent: str) -> int | None:
    normalized = str(user_agent or "")
    for marker in ("HeadlessChrome/", "Chrome/"):
        if marker not in normalized:
            continue
        major = normalized.split(marker, 1)[1].split(".", 1)[0]
        if major.isdigit() and 1 <= int(major) <= 999:
            return int(major)
    return None


def _body_fingerprint(html: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    for offset in range(0, len(html), 64 * 1024):
        chunk = html[offset : offset + (64 * 1024)].encode(
            "utf-8",
            errors="ignore",
        )
        byte_count += len(chunk)
        digest.update(chunk)
    return byte_count, digest.hexdigest()


def _build_unclassified_dom_diagnostic(
    result: _AttemptResult,
    *,
    requested_url: str,
    kind: str,
    wait_selector: str,
    input_cookies: list[dict],
) -> dict:
    html = str(result.html or "")
    body_bytes, body_sha256 = _body_fingerprint(html)
    html_sample = html[:_MAX_DIAGNOSTIC_HTML_CHARS]
    page = HtmlPageAdapter(html_sample)

    anchors = page.css("a[href]")
    scripts = page.css("script")
    catalog_links = sum(
        "/catalog/" in str(anchor.attrib.get("href") or "") for anchor in anchors
    )
    json_ld = sum(
        str(script.attrib.get("type") or "").strip().lower()
        == "application/ld+json"
        for script in scripts
    )
    ready_count = (
        catalog_links
        if wait_selector == "a[href*='/catalog/']"
        else json_ld
        if wait_selector == "script[type='application/ld+json']"
        else len(page.css(wait_selector))
    )
    raw_counts = {
        "ready": ready_count,
        "anchors": len(anchors),
        "catalog_links": catalog_links,
        "json_ld": json_ld,
        "scripts": len(scripts),
    }

    titles = page.css("title")
    title = str(titles[0].text or "") if titles else ""
    title_bytes = len(title.encode("utf-8", errors="ignore"))
    title_class = _fixed_class(
        title,
        (
            ("captcha", ("captcha",)),
            (
                "challenge",
                (
                    "aws waf",
                    "awswaf",
                    "human verification",
                    "checking your browser",
                ),
            ),
            ("access_denied", ("request blocked", "access denied", "forbidden")),
            ("recordcity", ("record city", "recordcity", "レコードシティ")),
        ),
    )

    main = []
    for entry in result.probe.main_responses:
        entry = entry if isinstance(entry, dict) else {}
        action = str(entry.get("action") or "").strip().lower()
        main.append(
            {
                "status": _status_value(entry.get("status")),
                "action": (
                    action
                    if action in {"challenge", "captcha"}
                    else "empty" if not action else "other"
                ),
                "server": _fixed_class(
                    entry.get("server"),
                    (("cloudfront", ("cloudfront",)),),
                ),
                "x_cache": _fixed_class(
                    entry.get("x_cache"),
                    (
                        ("error", ("error",)),
                        ("hit", ("hit",)),
                        ("miss", ("miss",)),
                    ),
                ),
            }
        )

    user_agent = str(result.probe.user_agent or "")
    browser_mode = (
        "headful" if result.probe.profile.endswith("/headful") else "headless"
    )
    expected_user_agent = (
        _chrome_major(user_agent) is not None
        if browser_mode == "headful"
        else user_agent == _CHROMIUM_145_LINUX_UA
    )
    page_errors = max(0, int(result.probe.page_error_count or 0))
    if result.probe.token_before and result.probe.token_after:
        token_transition = (
            "reused"
            if _cookie_values_match(input_cookies, result.waf_cookies)
            else "rotated"
        )
    elif result.probe.token_after:
        token_transition = "minted"
    elif result.probe.token_before:
        token_transition = "dropped"
    else:
        token_transition = "none"
    return {
        "reason": "RC_TARGET_DOM_MISSING_UNCLASSIFIED",
        "probe": result.probe.probe_id,
        "attempt": result.probe.attempt,
        "kind": kind if kind in {"search", "detail"} else "other",
        "ready": bool(result.probe.ready),
        "target_status": _status_value(result.probe.final_status),
        "main": main,
        "waf_statuses": [
            _status_value(entry.get("status"))
            for entry in result.probe.waf_responses
            if isinstance(entry, dict)
        ],
        "markers": {
            "challenge": _looks_like_waf_challenge(html_sample),
            "captcha": _looks_like_waf_captcha(html_sample),
            "request_blocked": "request blocked" in html_sample.lower(),
            "no_results": has_no_results_evidence(page.get_text(), _SITE),
        },
        "token": {
            "before": result.probe.token_before,
            "after": result.probe.token_after,
            "count": min(len(result.waf_cookies), 2),
            "transition": token_transition,
        },
        "final_url": _url_diagnostic(result.url, requested_url),
        "title": {
            "class": title_class,
            "present": bool(title.strip()),
            "bytes": min(title_bytes, _MAX_DIAGNOSTIC_TITLE_SIZE),
            "capped": title_bytes > _MAX_DIAGNOSTIC_TITLE_SIZE,
        },
        "body_bytes": min(body_bytes, _MAX_DIAGNOSTIC_BODY_BYTES),
        "body_bytes_capped": body_bytes > _MAX_DIAGNOSTIC_BODY_BYTES,
        "body_sha256": body_sha256,
        "dom_counts": {
            name: min(count, _MAX_DIAGNOSTIC_DOM_COUNT)
            for name, count in raw_counts.items()
        },
        "dom_counts_capped": any(
            count > _MAX_DIAGNOSTIC_DOM_COUNT for count in raw_counts.values()
        ),
        "dom_sample_chars": len(html_sample),
        "dom_sample_truncated": len(html) > len(html_sample),
        "browser": {
            "mode": browser_mode,
            "webdriver_true": result.probe.webdriver_true,
            "headless_ua": result.probe.headless_user_agent,
            "chrome_major": _chrome_major(user_agent),
            "language_ja": str(result.probe.language or "").lower().startswith("ja"),
            "profile_matches_expected": (
                result.probe.webdriver_true is False
                and result.probe.headless_user_agent is False
                and expected_user_agent
                and str(result.probe.language or "").lower().startswith("ja")
            ),
        },
        "page_errors": min(page_errors, _MAX_DIAGNOSTIC_DOM_COUNT),
        "page_errors_capped": page_errors > _MAX_DIAGNOSTIC_DOM_COUNT,
        "waf_request_failure_events_observed": min(
            len(result.probe.waf_request_failures), _MAX_EVENTS
        ),
        "cloudfront_request_ids": _safe_cloudfront_request_ids(
            result.probe.cloudfront_request_ids
        ),
        "navigation_error": (
            "none"
            if result.navigation_error is None
            else (
                "timeout"
                if type(result.navigation_error).__name__ == "TimeoutError"
                else "other"
            )
        ),
    }


def _log_unclassified_dom_missing(
    result: _AttemptResult,
    *,
    requested_url: str,
    kind: str,
    wait_selector: str,
    input_cookies: list[dict],
) -> None:
    try:
        diagnostic = _build_unclassified_dom_diagnostic(
            result,
            requested_url=requested_url,
            kind=kind,
            wait_selector=wait_selector,
            input_cookies=input_cookies,
        )
    except Exception:
        diagnostic = {
            "reason": "RC_TARGET_DOM_MISSING_UNCLASSIFIED",
            "diagnostic_error": True,
            "probe": result.probe.probe_id,
            "attempt": result.probe.attempt,
            "kind": kind if kind in {"search", "detail"} else "other",
        }
    logger.warning(
        "Record City unclassified target DOM missing: %s",
        json.dumps(
            diagnostic,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


async def _run_recordcity_attempt(
    normalized_url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int,
    wait_selector_timeout: int,
    network_idle: bool,
    input_cookies: list[dict],
    attempt: int,
    probe_id: str,
    headless: bool = True,
    launch_args: list[str] | None = None,
    context_options: dict | None = None,
    automation_backend: str = "patchright",
    channel: str | None = "chromium",
    runtime_site: str = _SITE,
    profile_label: str = "patchright/chromium/headless",
) -> _AttemptResult:
    probe = _WafProbe(
        probe_id=probe_id,
        attempt=attempt,
        profile=profile_label,
        token_before=bool(input_cookies),
    )
    page_state: dict[str, object] = {}

    async def _task(page, context):
        blocked_urls = await install_navigation_guard(context, _SITE, kind=kind)
        response_events: list[object] = []
        last_top_response: list[object | None] = [None]

        def _on_response(response) -> None:
            if not _should_capture_response(response):
                return
            response_url = str(getattr(response, "url", "") or "")
            if (
                _is_recordcity_host(response_url)
                and _is_top_level_document_response(response)
            ):
                last_top_response[0] = response
            response_events.append(response)
            if len(response_events) > (_MAX_EVENTS * 2):
                del response_events[0 : len(response_events) - (_MAX_EVENTS * 2)]

        def _on_page_error(_error) -> None:
            probe.page_error_count += 1

        event_handler = getattr(page, "on", None)
        if callable(event_handler):
            event_handler("response", _on_response)
            event_handler(
                "requestfailed",
                lambda request: _record_request_failure(probe, request),
            )
            event_handler("pageerror", _on_page_error)

        response = None
        navigation_error = None
        try:
            response = await page.goto(
                normalized_url,
                wait_until="domcontentloaded",
                timeout=max(1, int(timeout)),
            )
        except Exception as exc:
            raise_for_blocked_navigation(blocked_urls, _SITE)
            navigation_error = exc

        if navigation_error is None:
            raise_for_blocked_navigation(blocked_urls, _SITE)
            if network_idle:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=min(timeout, 5000),
                    )
                except Exception:
                    pass
            try:
                await page.wait_for_selector(
                    wait_selector,
                    state="attached",
                    timeout=min(timeout, max(1, int(wait_selector_timeout))),
                )
            except Exception:
                pass

        raise_for_blocked_navigation(blocked_urls, _SITE)

        # Drain only the relevant response objects retained by the synchronous
        # event callback. The loop notices events appended while header reads
        # yield, and the rolling list always preserves the newest response.
        event_index = 0
        while event_index < len(response_events):
            observed_response = response_events[event_index]
            event_index += 1
            try:
                await _record_response(probe, observed_response)
            except Exception:
                continue
        if last_top_response[0] is not None:
            # Keep the actual final top-level response decisive even when a
            # long token-resource sequence rolled it out of the trace buffer.
            await _record_response(probe, last_top_response[0], force_main=True)
        elif response is not None and not probe.main_responses:
            await _record_response(probe, response, force_main=True)

        try:
            html = await page.content()
        except Exception:
            html = ""
        await _capture_browser_signals(probe, page)
        try:
            cookies_after = await context.cookies()
        except Exception:
            cookies_after = []
        waf_cookies = _current_waf_cookies(cookies_after)
        probe.token_after = bool(waf_cookies)
        probe.token_cookie_metadata = [
            {
                "domain": str(cookie.get("domain") or "")[:120],
                "path": str(cookie.get("path") or "")[:120],
                "expires": cookie.get("expires"),
                "secure": bool(cookie.get("secure")),
                "same_site": str(cookie.get("sameSite") or "")[:20],
            }
            for cookie in waf_cookies
        ]

        final_url = str(getattr(page, "url", "") or normalized_url)
        validate_marketplace_url(final_url, _SITE, kind=kind)
        # A timed-out navigation can still leave the requested, usable DOM in
        # the page. Inspect the current document before deciding whether the
        # transport exception is fatal.
        probe.ready = _has_ready_evidence(html, wait_selector, kind=kind)
        if kind == "detail" and probe.ready:
            requested_sku = (
                str(urlparse(normalized_url).path or "")
                .rstrip("/")
                .rsplit("/", 1)[-1]
            )
            final_sku = (
                str(urlparse(final_url).path or "")
                .rstrip("/")
                .rsplit("/", 1)[-1]
            )
            # Selector presence alone is insufficient when a browser page is
            # reused or a redirect lands on a different catalog item.  The
            # final URL and Product JSON-LD must both identify the requested
            # item before a response can become a success.
            probe.ready = (
                final_sku == requested_sku
                and _contains_product_json_ld(html, requested_sku)
            )
        observed_status = probe.final_status or getattr(response, "status", None) or 200
        if probe.ready and observed_status in {202, 403, 405}:
            # A current Product/listing DOM is decisive even if a bounded event
            # trace missed the automatic 200 reload after the Challenge.
            observed_status = 200
        page_state["result"] = _AttemptResult(
            html=html,
            url=final_url,
            status=int(observed_status),
            probe=probe,
            waf_cookies=waf_cookies,
            navigation_error=navigation_error,
        )

    await run_browser_page_task(
        runtime_site,
        _task,
        headless=headless,
        launch_args=_LAUNCH_ARGS if launch_args is None else launch_args,
        context_options=_context_options(input_cookies, context_options),
        automation_backend=automation_backend,
        channel=channel,
    )
    result = page_state.get("result")
    if not isinstance(result, _AttemptResult):
        raise RuntimeError("Record Cityブラウザ取得結果を受け取れませんでした。")
    return result


_RETRYABLE_CHALLENGE_REASONS = frozenset(
    {
        "RC_WAF_TOKEN_PRESENT_CHALLENGE_CONTINUED",
        "RC_WAF_CHALLENGE_SCRIPT_FAILED",
        "RC_WAF_CHALLENGE_NO_TOKEN",
    }
)


def _production_browser_settings() -> dict:
    raw_profile = str(os.environ.get(_PRODUCTION_PROFILE_ENV, "headless") or "headless")
    normalized = raw_profile.strip().lower()
    aliases = {
        "patchright-current": "headless",
        "patchright-headless": "headless",
        "patchright-headful": "headful",
    }
    normalized = aliases.get(normalized, normalized)
    settings = _PRODUCTION_BROWSER_PROFILES.get(normalized)
    if settings is None:
        raise ScrapeHttpError(
            "レコードシティのブラウザ設定が不正です"
            "（reason=RC_BROWSER_PROFILE_CONFIG_INVALID）。"
        )
    if normalized == "headful" and not str(os.environ.get("DISPLAY") or "").strip():
        raise ScrapeHttpError(
            "レコードシティのheadfulブラウザを起動できません"
            "（reason=RC_HEADFUL_DISPLAY_UNAVAILABLE）。"
        )
    return copy.deepcopy(settings)


def _cookie_values_match(left: list[dict], right: list[dict]) -> bool:
    def _signature(cookies):
        return sorted(
            (
                str(cookie.get("name") or ""),
                str(cookie.get("domain") or ""),
                str(cookie.get("path") or ""),
                str(cookie.get("value") or ""),
            )
            for cookie in cookies
        )

    return _signature(left) == _signature(right)


def _retry_cookies(
    input_cookies: list[dict],
    result: _AttemptResult,
    reason_code: str,
) -> list[dict] | None:
    if result.probe.final_status == 429:
        # A target-side rate limit is a stop signal even when the response
        # also includes Challenge metadata or a newly minted token.
        return None
    if reason_code not in _RETRYABLE_CHALLENGE_REASONS:
        return None
    if result.waf_cookies:
        if input_cookies and _cookie_values_match(input_cookies, result.waf_cookies):
            # The cached token survived unchanged but did not reach product
            # HTML. Retry once from a fresh context so WAF can mint a new one.
            return []
        # A newly minted/updated token gets exactly one explicit resubmission.
        return copy.deepcopy(result.waf_cookies)
    if input_cookies:
        return []
    return None


async def _fetch_recordcity_page_unlocked(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int = 45000,
    wait_selector_timeout: int = 20000,
    network_idle: bool = True,
) -> HtmlPageAdapter:
    request_kind, site = classify_target_url(url)
    classified_kind = "detail" if request_kind == "item" else "search"
    if site != _SITE or kind != classified_kind:
        raise ValueError("Record CityのURL種別が一致しません。")
    normalized_url = validate_marketplace_url(url, _SITE, kind=kind)
    browser_settings = _production_browser_settings()

    cached_cookies = _cached_waf_cookies()
    input_cookies = cached_cookies
    probe_id = secrets.token_hex(4)
    for attempt in (1, 2):
        result = await _run_recordcity_attempt(
            normalized_url,
            kind=kind,
            wait_selector=wait_selector,
            timeout=timeout,
            wait_selector_timeout=wait_selector_timeout,
            network_idle=network_idle,
            input_cookies=input_cookies,
            attempt=attempt,
            probe_id=probe_id,
            headless=bool(browser_settings["headless"]),
            context_options=browser_settings["context_options"],
            runtime_site=str(browser_settings["runtime_site"]),
            profile_label=str(browser_settings["profile_label"]),
        )
        failure = _classify_waf_failure(result.probe, result.html)
        if failure is not None:
            reason_code, explanation = failure
            retry_cookies = (
                _retry_cookies(input_cookies, result, reason_code)
                if attempt == 1
                else None
            )
            if retry_cookies is not None:
                logger.info(
                    "Record City WAF probe retrying once: probe=%s attempt=%d "
                    "profile=%s reason=%s "
                    "main=%s waf=%s token_before=%s token_after=%s "
                    "retry_with_token=%s cloudfront_request_ids=%s",
                    result.probe.probe_id,
                    attempt,
                    result.probe.profile,
                    reason_code,
                    result.probe.main_responses,
                    result.probe.waf_responses,
                    result.probe.token_before,
                    result.probe.token_after,
                    bool(retry_cookies),
                    result.probe.cloudfront_request_ids,
                )
                input_cookies = retry_cookies
                continue
            if cached_cookies:
                _discard_cached_waf_cookies()
            _raise_waf_failure(result.probe, reason_code, explanation)

        if result.navigation_error is not None and not result.probe.ready:
            _log_unclassified_dom_missing(
                result,
                requested_url=normalized_url,
                kind=kind,
                wait_selector=wait_selector,
                input_cookies=input_cookies,
            )
            if cached_cookies:
                _discard_cached_waf_cookies()
            raise result.navigation_error

        if result.probe.ready:
            _remember_waf_cookies(result.waf_cookies)
            _log_waf_success(result.probe)
        page = HtmlPageAdapter(
            result.html,
            url=result.url,
            status=result.status,
        )
        validate_fetch_response(page, _SITE, kind=kind)
        if not result.probe.ready:
            _log_unclassified_dom_missing(
                result,
                requested_url=normalized_url,
                kind=kind,
                wait_selector=wait_selector,
                input_cookies=input_cookies,
            )
            if kind == "detail" and _contains_product_json_ld(result.html):
                raise ScrapeSelectorDriftError(
                    "レコードシティの商品識別情報が要求URLと一致しませんでした"
                    "（reason=RC_DETAIL_IDENTITY_MISMATCH）。"
                )
        return page

    raise RuntimeError("Record Cityの制御された再試行が完了しませんでした。")


_BROWSER_PROBE_PROFILES: dict[str, dict] = {
    # Resolved from RECORDCITY_BROWSER_PROFILE at call time so this remains an
    # exact production control after a deployment-profile change.
    "patchright-current": {"production": True},
    # Compatibility alias for the profile that the Render A/B probe proved and
    # production now uses. Keeping the explicit name makes old probe commands
    # and stored diagnostic results comparable.
    "patchright-headless-ua": {
        "headless": True,
        "context_options": {"user_agent": _CHROMIUM_145_LINUX_UA},
    },
    # Patchright documents headful Chrome/Chromium as its supported stealth
    # path on Linux. Run the CLI under Xvfb; native UA/client hints are kept.
    "patchright-headful": {
        "headless": False,
        "context_options": {"no_viewport": True},
    },
    # A second headful cell changes only browser locale/timezone presentation.
    # It is opt-in because every cell causes another target navigation.
    "patchright-headful-tokyo": {
        "headless": False,
        "context_options": {
            "no_viewport": True,
            "timezone_id": "Asia/Tokyo",
        },
    },
    # The proxy cells complete the IP x browser-mode comparison. Credentials
    # are read from RECORDCITY_PROXY_URL and passed only to BrowserContext.
    "patchright-headless-proxy": {
        "headless": True,
        "context_options": _PRODUCTION_CONTEXT_OVERRIDES,
        "proxy": True,
    },
    "patchright-headful-proxy": {
        "headless": False,
        "context_options": {"no_viewport": True},
        "proxy": True,
    },
}


def recordcity_browser_probe_profiles() -> tuple[str, ...]:
    return tuple(_BROWSER_PROBE_PROFILES)


def _browser_proxy_options(proxy_url: str) -> dict:
    """Build Playwright proxy options without returning a credential URL."""
    try:
        parsed = urlparse(str(proxy_url or "").strip())
        host = str(parsed.hostname or "").strip()
        port = parsed.port
    except (TypeError, ValueError):
        host = ""
        parsed = None
        port = None
    if (
        parsed is None
        or parsed.scheme.lower() not in {"http", "https", "socks5"}
        or not host
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("RECORDCITY_PROXY_URLの形式が不正です。")
    display_host = f"[{host}]" if ":" in host else host
    server = f"{parsed.scheme.lower()}://{display_host}"
    if port is not None:
        server += f":{port}"
    options = {"server": server}
    if parsed.username is not None:
        options["username"] = unquote(parsed.username)
    if parsed.password is not None:
        options["password"] = unquote(parsed.password)
    return options


async def probe_recordcity_browser_once_async(
    url: str,
    *,
    profile: str,
    timeout: int = 45000,
    wait_selector_timeout: int = 20000,
) -> dict:
    """Run one isolated browser cell and return only secret-free evidence.

    This deliberately bypasses the production token cache and controlled
    retry. The AWS interstitial may reload the page itself, but this helper
    never starts a second browser attempt, which keeps comparison cells
    bounded and independently attributable.
    """
    settings = _BROWSER_PROBE_PROFILES.get(str(profile or ""))
    if settings is None:
        supported = ", ".join(recordcity_browser_probe_profiles())
        raise ValueError(f"Unsupported Record City browser probe profile: {profile} ({supported})")

    request_kind, site = classify_target_url(url)
    if site != _SITE or request_kind not in {"item", "search"}:
        raise ValueError("Record CityのURLを指定してください。")
    kind = "detail" if request_kind == "item" else "search"
    normalized_url = validate_marketplace_url(url, _SITE, kind=kind)
    wait_selector = (
        "script[type='application/ld+json']"
        if kind == "detail"
        else "a[href*='/catalog/']"
    )

    if settings.get("production"):
        settings = _production_browser_settings()
    else:
        settings = copy.deepcopy(settings)

    profile_context_options = dict(settings.get("context_options") or {})
    if settings.get("proxy"):
        proxy_url = str(os.environ.get("RECORDCITY_PROXY_URL") or "").strip()
        if not proxy_url:
            raise ValueError("RECORDCITY_PROXY_URLが設定されていません。")
        profile_context_options["proxy"] = _browser_proxy_options(proxy_url)

    started = time.monotonic()
    result = await _run_recordcity_attempt(
        normalized_url,
        kind=kind,
        wait_selector=wait_selector,
        timeout=timeout,
        wait_selector_timeout=wait_selector_timeout,
        network_idle=True,
        input_cookies=[],
        attempt=1,
        probe_id=secrets.token_hex(4),
        headless=bool(settings["headless"]),
        launch_args=[],
        context_options=profile_context_options,
        automation_backend="patchright",
        channel="chromium",
        runtime_site=f"recordcity_probe_{profile.replace('-', '_')}",
        profile_label=f"patchright/chromium/{'headless' if settings['headless'] else 'headful'}",
    )
    failure = _classify_waf_failure(result.probe, result.html)
    actions = [
        str(entry.get("action") or "")
        for entry in result.probe.main_responses
        if str(entry.get("action") or "")
    ]
    challenge = _looks_like_waf_challenge(result.html) or "challenge" in actions
    captcha = _looks_like_waf_captcha(result.html) or "captcha" in actions
    body_bytes = str(result.html or "").encode("utf-8", errors="ignore")
    return {
        "strategy": profile,
        "browser_mode": "headless" if settings["headless"] else "headful",
        "attempted": True,
        "transport_status": None,
        "target_status": result.status,
        "header_source": "browser",
        "waf_action": result.probe.final_action,
        "waf_actions_seen": list(dict.fromkeys(actions)),
        "server": str((result.probe.last_main_response or {}).get("server") or ""),
        "x_cache": str((result.probe.last_main_response or {}).get("x_cache") or ""),
        "cloudfront_request_ids": list(result.probe.cloudfront_request_ids),
        "main_responses": copy.deepcopy(result.probe.main_responses),
        "waf_responses": copy.deepcopy(result.probe.waf_responses),
        "challenge": bool(challenge),
        "captcha": bool(captcha),
        "blocked_marker": "request blocked" in str(result.html or "").lower(),
        "aws_waf_token": bool(result.probe.token_after),
        "ready_dom": bool(result.probe.ready),
        "product_json_ld": _contains_product_json_ld(
            result.html,
            str(urlparse(normalized_url).path or "")
            .rstrip("/")
            .rsplit("/", 1)[-1]
            if kind == "detail"
            else None,
        ),
        "body_bytes": len(body_bytes),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "webdriver_true": result.probe.webdriver_true,
        "headless_user_agent": result.probe.headless_user_agent,
        "user_agent": result.probe.user_agent,
        "language": result.probe.language,
        "timezone": result.probe.timezone,
        "platform": result.probe.platform,
        "screen": result.probe.screen,
        "webgl_vendor": result.probe.webgl_vendor,
        "webgl_renderer": result.probe.webgl_renderer,
        "failure_reason": failure[0] if failure else "",
        "navigation_error": (
            type(result.navigation_error).__name__
            if result.navigation_error is not None
            else ""
        ),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def probe_recordcity_browser_once_sync(
    url: str,
    *,
    profile: str,
    timeout: int = 45000,
    wait_selector_timeout: int = 20000,
) -> dict:
    return run_coro_sync(
        probe_recordcity_browser_once_async(
            url,
            profile=profile,
            timeout=timeout,
            wait_selector_timeout=wait_selector_timeout,
        )
    )


async def fetch_recordcity_page_via_browser_pool_async(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int = 45000,
    wait_selector_timeout: int = 20000,
    network_idle: bool = True,
) -> HtmlPageAdapter:
    # BrowserContext creation is serialized for this site so a second request
    # cannot snapshot an old token while the first is refreshing it. A
    # non-blocking poll keeps this safe across the separate event loops used by
    # synchronous worker calls.
    acquired = False
    try:
        while not acquired:
            acquired = _FETCH_LOCK.acquire(blocking=False)
            if not acquired:
                await asyncio.sleep(0.02)
        return await _fetch_recordcity_page_unlocked(
            url,
            kind=kind,
            wait_selector=wait_selector,
            timeout=timeout,
            wait_selector_timeout=wait_selector_timeout,
            network_idle=network_idle,
        )
    finally:
        if acquired:
            _FETCH_LOCK.release()


def fetch_recordcity_page_via_browser_pool_sync(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int = 45000,
    wait_selector_timeout: int = 20000,
    network_idle: bool = True,
) -> HtmlPageAdapter:
    return run_coro_sync(
        fetch_recordcity_page_via_browser_pool_async(
            url,
            kind=kind,
            wait_selector=wait_selector,
            timeout=timeout,
            wait_selector_timeout=wait_selector_timeout,
            network_idle=network_idle,
        )
    )
