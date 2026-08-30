"""Record City browser fetches with AWS WAF-aware diagnostics.

Record City is the only supported marketplace that currently returns an AWS
WAF Challenge interstitial.  Keep the patched browser profile, token reuse,
and diagnostic detail here so Mercari, Surugaya, SNKRDUNK, and the generic
dynamic fetch path retain their existing behaviour.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from services.browser_pool import run_browser_page_task
from services.html_page_adapter import HtmlPageAdapter
from services.scrape_request import classify_target_url
from services.scrape_safety import (
    ScrapeBlockedError,
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

# Patchright removes Playwright's automation flags itself.  Passing ESP's
# ordinary ``--disable-extensions`` flag would put one of those fingerprints
# back, so an explicit empty list is intentional.
_LAUNCH_ARGS: list[str] = []
_CONTEXT_OPTIONS = {"locale": "ja-JP"}

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


def _context_options(cookies: list[dict]) -> dict:
    options = copy.deepcopy(_CONTEXT_OPTIONS)
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
    waf_cookies: list[dict]
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
            """() => ({
                webdriverTrue: navigator.webdriver === true,
                headlessUserAgent: /HeadlessChrome/i.test(navigator.userAgent || ''),
                userAgent: navigator.userAgent || '',
                language: navigator.language || '',
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || ''
            })"""
        )
    except Exception:
        return
    if isinstance(signals, dict):
        probe.webdriver_true = bool(signals.get("webdriverTrue"))
        probe.headless_user_agent = bool(signals.get("headlessUserAgent"))
        probe.user_agent = str(signals.get("userAgent") or "")[:240]
        probe.language = str(signals.get("language") or "")[:40]
        probe.timezone = str(signals.get("timezone") or "")[:80]


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


def _has_ready_evidence(html: str, wait_selector: str) -> bool:
    return bool(HtmlPageAdapter(str(html or "")).css(wait_selector))


def _classify_waf_failure(probe: _WafProbe, html: str) -> tuple[str, str] | None:
    """Return a stable reason code and operator-facing Japanese explanation."""
    # Current product/listing DOM is stronger evidence than a bounded history
    # containing an earlier 202 Challenge response.
    if probe.ready:
        return None

    status = probe.final_status
    final_action = probe.final_action
    challenge_html = _looks_like_waf_challenge(html)
    challenge_unresolved = (
        final_action == "challenge"
        or status == 202
        or challenge_html
    )
    captcha_seen = (
        final_action == "captcha"
        or status == 405
        or _looks_like_waf_captcha(html)
    )

    if captcha_seen:
        return (
            "RC_WAF_CAPTCHA_REQUIRED",
            "AWS WAFがCAPTCHAを要求したため、自動取得を続行できませんでした",
        )
    if status == 403:
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
        "profile=patchright/chromium/headless reason=%s main=%s waf=%s "
        "token_before=%s token_after=%s webdriver_true=%s headless_ua=%s "
        "user_agent=%s language=%s timezone=%s token_cookie_metadata=%s "
        "waf_request_failures=%s page_errors=%d "
        "cloudfront_request_ids=%s",
        probe.probe_id,
        probe.attempt,
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
        "profile=patchright/chromium/headless main=%s waf=%s "
        "token_before=%s token_after=%s webdriver_true=%s headless_ua=%s "
        "user_agent=%s language=%s timezone=%s token_cookie_metadata=%s "
        "waf_request_failures=%s page_errors=%d cloudfront_request_ids=%s",
        probe.probe_id,
        probe.attempt,
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
) -> _AttemptResult:
    probe = _WafProbe(
        probe_id=probe_id,
        attempt=attempt,
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
        probe.ready = (
            navigation_error is None
            and _has_ready_evidence(html, wait_selector)
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
        _SITE,
        _task,
        headless=True,
        launch_args=_LAUNCH_ARGS,
        context_options=_context_options(input_cookies),
        automation_backend="patchright",
        channel="chromium",
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
                    "profile=patchright/chromium/headless reason=%s "
                    "main=%s waf=%s token_before=%s token_after=%s "
                    "retry_with_token=%s cloudfront_request_ids=%s",
                    result.probe.probe_id,
                    attempt,
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

        if result.navigation_error is not None:
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
        return page

    raise RuntimeError("Record Cityの制御された再試行が完了しませんでした。")


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
