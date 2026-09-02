import asyncio
import hashlib
import json
import logging
import time

import pytest

from services import recordcity_browser_fetch as recordcity_fetch
from services.scrape_safety import ScrapeBlockedError


DETAIL_URL = "https://www.recordcity.jp/catalog/4936480"
READY_SELECTOR = "script[type='application/ld+json']"
PRODUCT_HTML = """<html><body>
<script type="application/ld+json">{"@type":"Product","name":"Sunrise","sku":"4936480"}</script>
</body></html>"""
CHALLENGE_HTML = """<html><script>
window.gokuProps = {};
AwsWafIntegration.fetch('https://example.token.awswaf.com/challenge.js');
</script></html>"""
NORMAL_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
PRODUCTION_CONTEXT_OPTIONS = {
    "locale": "ja-JP",
    "user_agent": NORMAL_CHROME_UA,
}


class FakeRequest:
    def __init__(self, url, resource_type="document", *, frame=None):
        self.url = url
        self.resource_type = resource_type
        self.frame = frame if frame is not None else FakeFrame()

    def is_navigation_request(self):
        return self.resource_type == "document"


class FakeFrame:
    def __init__(self, parent=None):
        self.parent_frame = parent


class FakeResponse:
    def __init__(
        self,
        status,
        *,
        url=DETAIL_URL,
        headers=None,
        resource_type="document",
        frame_parent=None,
    ):
        self.status = status
        self.url = url
        self.request = FakeRequest(
            url,
            resource_type=resource_type,
            frame=FakeFrame(parent=frame_parent),
        )
        self._headers = headers or {}

    async def all_headers(self):
        return dict(self._headers)


class FakeContext:
    def __init__(self, cookies=()):
        self._cookies = list(cookies)
        self.routes = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def cookies(self):
        return list(self._cookies)


class FakePage:
    def __init__(
        self,
        response,
        *,
        html=PRODUCT_HTML,
        selector_ready=True,
        reload_response=None,
        wait_responses=(),
        final_html=None,
        goto_error=None,
    ):
        self._response = response
        self._html = html
        self._selector_ready = selector_ready
        self._wait_responses = list(wait_responses)
        if reload_response is not None:
            self._wait_responses.append(reload_response)
        self._final_html = PRODUCT_HTML if reload_response is not None else final_html
        self._goto_error = goto_error
        self._handlers = {}
        self.selector_calls = []
        self.url = DETAIL_URL

    def on(self, name, handler):
        self._handlers[name] = handler

    async def goto(self, _url, **_kwargs):
        handler = self._handlers.get("response")
        if handler:
            handler(self._response)
        if self._goto_error is not None:
            raise self._goto_error
        return self._response

    async def wait_for_load_state(self, _state, **_kwargs):
        return None

    async def wait_for_selector(self, selector, **kwargs):
        self.selector_calls.append({"selector": selector, **kwargs})
        if self._wait_responses:
            handler = self._handlers.get("response")
            for response in self._wait_responses:
                if handler:
                    handler(response)
            self._response = self._wait_responses[-1]
        if self._final_html is not None:
            self._html = self._final_html
            self._selector_ready = True
        if not self._selector_ready:
            raise TimeoutError("selector missing")
        return object()

    async def content(self):
        return self._html

    async def evaluate(self, _script):
        return {
            "webdriverTrue": False,
            "headlessUserAgent": False,
            "userAgent": "Mozilla/5.0 Chrome/145.0.0.0 Safari/537.36",
            "language": "ja-JP",
            "timezone": "UTC",
        }


def _waf_cookie(value="secret-token"):
    return {
        "name": "aws-waf-token",
        "value": value,
        "domain": ".recordcity.jp",
        "path": "/",
        "expires": time.time() + 3600,
        "httpOnly": False,
        "secure": True,
        "sameSite": "Lax",
    }


@pytest.fixture(autouse=True)
def clear_waf_cookie_cache():
    recordcity_fetch._discard_cached_waf_cookies()
    yield
    recordcity_fetch._discard_cached_waf_cookies()


def _install_browser_fake(monkeypatch, pages_and_contexts, captured):
    queue = list(pages_and_contexts)

    async def fake_run_browser_page_task(site, task, **kwargs):
        page, context = queue.pop(0)
        captured.append({"site": site, **kwargs})
        return await task(page, context)

    monkeypatch.setattr(
        recordcity_fetch,
        "run_browser_page_task",
        fake_run_browser_page_task,
    )


def _fetch():
    return asyncio.run(
        recordcity_fetch.fetch_recordcity_page_via_browser_pool_async(
            DETAIL_URL,
            kind="detail",
            wait_selector=READY_SELECTOR,
        )
    )


def _fetch_search(url):
    return asyncio.run(
        recordcity_fetch.fetch_recordcity_page_via_browser_pool_async(
            url,
            kind="search",
            wait_selector="a[href*='/catalog/']",
        )
    )


def test_search_no_results_text_is_ready_evidence():
    assert recordcity_fetch._has_ready_evidence(
        "<html><body>検索結果がありません</body></html>",
        "a[href*='/catalog/']",
        kind="search",
    )


def test_unclassified_url_diagnostic_keeps_valid_shape_when_query_is_capped():
    query = "&".join(f"field{index}=secret{index}" for index in range(129))
    url = f"https://www.recordcity.jp/en/catalog/4936480?{query}"

    diagnostic = recordcity_fetch._url_diagnostic(url, url)

    assert diagnostic == {
        "exact_url_match": True,
        "fragment_present": False,
        "host": "www",
        "keyword_present": None,
        "path": "catalog_detail",
        "query_parse": "capped",
        "query_present": True,
    }
    assert "secret" not in repr(diagnostic)


def test_unclassified_body_diagnostic_hashes_full_body_but_bounds_dom_sample():
    html = "<html><title>Record City</title><body>" + (
        "x" * (recordcity_fetch._MAX_DIAGNOSTIC_HTML_CHARS + 100)
    ) + "</body></html>"
    result = recordcity_fetch._AttemptResult(
        html=html,
        url=DETAIL_URL,
        status=200,
        probe=recordcity_fetch._WafProbe(probe_id="1234abcd"),
        waf_cookies=[],
    )

    diagnostic = recordcity_fetch._build_unclassified_dom_diagnostic(
        result,
        requested_url=DETAIL_URL,
        kind="detail",
        wait_selector=READY_SELECTOR,
        input_cookies=[],
    )

    assert diagnostic["dom_sample_chars"] == recordcity_fetch._MAX_DIAGNOSTIC_HTML_CHARS
    assert diagnostic["dom_sample_truncated"] is True
    assert diagnostic["body_bytes"] == len(html.encode("utf-8"))
    assert diagnostic["body_sha256"] == hashlib.sha256(
        html.encode("utf-8")
    ).hexdigest()


def test_recordcity_uses_site_scoped_patchright_profile(monkeypatch):
    captured = []
    page = FakePage(FakeResponse(200, headers={"server": "CloudFront"}))
    context = FakeContext()
    _install_browser_fake(monkeypatch, [(page, context)], captured)

    result = _fetch()

    assert result.status == 200
    assert "Product" in result.body
    assert captured == [
        {
            "site": "recordcity",
            "headless": True,
            "launch_args": [],
            "context_options": PRODUCTION_CONTEXT_OPTIONS,
            "automation_backend": "patchright",
            "channel": "chromium",
        }
    ]
    assert context.routes[0][0] == "**/*"
    assert page.selector_calls == [
        {
            "selector": READY_SELECTOR,
            "state": "attached",
            "timeout": 20000,
        }
    ]


def test_unready_unclassified_response_logs_one_secret_free_warning(
    monkeypatch,
    caplog,
):
    search_secret = "query-secret-not-for-logs"
    title_secret = "title-secret-not-for-logs"
    body_secret = "body-secret-not-for-logs"
    cookie_secret = "cookie-secret-not-for-logs"
    header_secret = "header-secret-not-for-logs"
    search_url = (
        "https://www.recordcity.jp/ja/catalog?keyword="
        f"{search_secret}"
    )
    html = (
        "<html><head><title>"
        f"{title_secret}\r\nFORGED\x1b[31m\u202e"
        "</title></head><body>"
        f"{body_secret}"
        "</body></html>"
    )
    response = FakeResponse(
        200,
        url=search_url,
        headers={
            "server": f"CloudFront\r\n{header_secret}",
            "x-cache": header_secret,
            "x-amz-cf-id": "diagnostic-request-id",
        },
    )
    page = FakePage(response, html=html, selector_ready=False)
    page.url = search_url
    captured = []
    _install_browser_fake(
        monkeypatch,
        [(page, FakeContext([_waf_cookie(cookie_secret)]))],
        captured,
    )

    with caplog.at_level(logging.WARNING):
        result = _fetch_search(search_url)

    assert result.status == 200
    records = [
        record
        for record in caplog.records
        if "RC_TARGET_DOM_MISSING_UNCLASSIFIED" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    prefix = "Record City unclassified target DOM missing: "
    assert records[0].getMessage().startswith(prefix)
    diagnostic = json.loads(records[0].getMessage()[len(prefix):])
    assert diagnostic["reason"] == "RC_TARGET_DOM_MISSING_UNCLASSIFIED"
    assert diagnostic["kind"] == "search"
    assert diagnostic["ready"] is False
    assert diagnostic["target_status"] == 200
    assert diagnostic["main"] == [
        {
            "action": "empty",
            "server": "cloudfront",
            "status": 200,
            "x_cache": "other",
        }
    ]
    assert diagnostic["token"] == {
        "after": True,
        "before": False,
        "count": 1,
        "transition": "minted",
    }
    assert diagnostic["final_url"] == {
        "fragment_present": False,
        "host": "www",
        "keyword_present": True,
        "path": "catalog_search",
        "query_present": True,
        "query_parse": "ok",
        "exact_url_match": True,
    }
    assert diagnostic["title"]["present"] is True
    assert diagnostic["title"]["class"] == "other"
    assert diagnostic["title"]["bytes"] > 0
    assert diagnostic["body_bytes"] == len(html.encode("utf-8"))
    assert diagnostic["body_sha256"] == hashlib.sha256(
        html.encode("utf-8")
    ).hexdigest()
    assert diagnostic["dom_sample_truncated"] is False
    assert diagnostic["markers"]["no_results"] is False
    assert diagnostic["dom_counts"] == {
        "anchors": 0,
        "catalog_links": 0,
        "json_ld": 0,
        "ready": 0,
        "scripts": 0,
    }
    assert diagnostic["browser"]["webdriver_true"] is False
    assert diagnostic["browser"]["headless_ua"] is False
    assert diagnostic["browser"]["chrome_major"] == 145
    assert diagnostic["cloudfront_request_ids"] == ["diagnostic-request-id"]

    for secret in (
        search_secret,
        title_secret,
        body_secret,
        cookie_secret,
        header_secret,
        "keyword=",
        search_url,
        "FORGED",
    ):
        assert secret not in caplog.text
    assert "\r" not in records[0].getMessage()
    assert "\x1b" not in records[0].getMessage()
    assert "\u202e" not in records[0].getMessage()


def test_ready_response_does_not_log_unclassified_diagnostic(monkeypatch, caplog):
    captured = []
    page = FakePage(FakeResponse(200), html=PRODUCT_HTML)
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    with caplog.at_level(logging.INFO):
        _fetch()

    assert "RC_TARGET_DOM_MISSING_UNCLASSIFIED" not in caplog.text


def test_classified_waf_response_does_not_log_unclassified_diagnostic(
    monkeypatch,
    caplog,
):
    captured = []
    page = FakePage(
        FakeResponse(202, headers={"x-amzn-waf-action": "challenge"}),
        html=CHALLENGE_HTML,
        selector_ready=False,
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ScrapeBlockedError, match="RC_WAF_CHALLENGE_NO_TOKEN"):
            _fetch()

    assert "Record City WAF probe failed" in caplog.text
    assert "RC_TARGET_DOM_MISSING_UNCLASSIFIED" not in caplog.text


def test_patchright_timeout_name_is_classified_without_logging_message(
    monkeypatch,
    caplog,
):
    timeout_secret = "timeout-message-secret-not-for-logs"
    patchright_timeout = type("TimeoutError", (Exception,), {})
    captured = []
    page = FakePage(
        FakeResponse(200),
        html="<html><body>ordinary response</body></html>",
        selector_ready=False,
        goto_error=patchright_timeout(timeout_secret),
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(patchright_timeout):
            _fetch()

    records = [
        record
        for record in caplog.records
        if "RC_TARGET_DOM_MISSING_UNCLASSIFIED" in record.getMessage()
    ]
    assert len(records) == 1
    diagnostic = json.loads(records[0].getMessage().split(": ", 1)[1])
    assert diagnostic["navigation_error"] == "timeout"
    assert timeout_secret not in caplog.text


def test_explicit_search_no_results_does_not_log_unclassified_diagnostic(
    monkeypatch,
    caplog,
):
    search_url = "https://www.recordcity.jp/ja/catalog?keyword=none"
    page = FakePage(
        FakeResponse(200, url=search_url),
        html="<html><body>検索結果がありません</body></html>",
        selector_ready=False,
    )
    page.url = search_url
    captured = []
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    with caplog.at_level(logging.INFO):
        result = _fetch_search(search_url)

    assert result.status == 200
    assert "RC_TARGET_DOM_MISSING_UNCLASSIFIED" not in caplog.text


def test_challenge_reload_to_product_is_success_and_caches_token(
    monkeypatch,
    caplog,
):
    captured = []
    challenge = FakeResponse(
        202,
        headers={"x-amzn-waf-action": "challenge", "server": "CloudFront"},
    )
    product = FakeResponse(200, headers={"server": "CloudFront"})
    cookie = _waf_cookie()
    page = FakePage(
        challenge,
        html=CHALLENGE_HTML,
        selector_ready=False,
        reload_response=product,
    )
    context = FakeContext([cookie])
    _install_browser_fake(monkeypatch, [(page, context)], captured)

    with caplog.at_level(logging.INFO):
        result = _fetch()

    assert result.status == 200
    assert "Product" in result.body
    assert recordcity_fetch._cached_waf_cookies()[0]["value"] == "secret-token"
    assert "Record City WAF fetch passed" in caplog.text
    assert "profile=patchright/chromium/headless" in caplog.text
    assert "secret-token" not in caplog.text


def test_final_product_response_survives_more_than_trace_limit(monkeypatch):
    captured = []
    challenge = FakeResponse(
        202,
        headers={"x-amzn-waf-action": "challenge", "x-cache": "loop-0"},
    )
    loop_responses = [
        FakeResponse(
            202,
            headers={
                "x-amzn-waf-action": "challenge",
                "x-cache": f"loop-{index}",
            },
        )
        for index in range(1, 15)
    ]
    loop_responses.append(FakeResponse(200, headers={"x-cache": "origin"}))
    page = FakePage(
        challenge,
        html=CHALLENGE_HTML,
        selector_ready=False,
        wait_responses=loop_responses,
        final_html=PRODUCT_HTML,
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext([_waf_cookie()]))], captured)

    result = _fetch()

    assert result.status == 200
    assert "Product" in result.body


def test_same_origin_subframe_does_not_overwrite_top_level_waf_status(monkeypatch):
    captured = []
    top_level = FakeResponse(403, headers={"server": "CloudFront"})
    subframe = FakeResponse(200, frame_parent=FakeFrame())
    page = FakePage(
        top_level,
        html="Request blocked.",
        selector_ready=False,
        wait_responses=[subframe],
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    with pytest.raises(ScrapeBlockedError, match="RC_WAF_BLOCK_403") as exc_info:
        _fetch()

    assert exc_info.value.status_code == 403


def test_goto_timeout_preserves_observed_waf_reason_and_does_not_cache_token(
    monkeypatch,
):
    captured = []
    page = FakePage(
        FakeResponse(202, headers={"x-amzn-waf-action": "challenge"}),
        html=CHALLENGE_HTML,
        selector_ready=False,
        goto_error=TimeoutError("navigation timed out"),
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    with pytest.raises(ScrapeBlockedError, match="RC_WAF_CHALLENGE_NO_TOKEN"):
        _fetch()

    assert recordcity_fetch._cached_waf_cookies() == []


def test_goto_timeout_accepts_product_dom_left_in_page(monkeypatch):
    captured = []
    page = FakePage(
        FakeResponse(200, headers={"server": "CloudFront"}),
        html=PRODUCT_HTML,
        goto_error=TimeoutError("navigation timed out"),
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    result = _fetch()

    assert result.status == 200
    assert "Product" in result.body


def test_generic_403_is_not_given_an_aws_waf_reason(monkeypatch):
    captured = []
    page = FakePage(
        FakeResponse(403, headers={"server": "nginx"}),
        html="<html><body>Forbidden</body></html>",
        selector_ready=False,
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    result = asyncio.run(
        recordcity_fetch.probe_recordcity_browser_once_async(
            DETAIL_URL,
            profile="patchright-current",
        )
    )

    assert result["target_status"] == 403
    assert result["failure_reason"] == ""
    assert result["challenge"] is False
    assert result["blocked_marker"] is False


def test_new_token_is_resubmitted_once_before_challenge_continued_reason(monkeypatch):
    captured = []
    first_page = FakePage(
        FakeResponse(202, headers={"x-amzn-waf-action": "challenge"}),
        html=CHALLENGE_HTML,
        selector_ready=False,
    )
    second_page = FakePage(
        FakeResponse(202, headers={"x-amzn-waf-action": "challenge"}),
        html=CHALLENGE_HTML,
        selector_ready=False,
    )
    cookie = _waf_cookie("new-token")
    _install_browser_fake(
        monkeypatch,
        [(first_page, FakeContext([cookie])), (second_page, FakeContext([cookie]))],
        captured,
    )

    with pytest.raises(
        ScrapeBlockedError,
        match="RC_WAF_TOKEN_PRESENT_CHALLENGE_CONTINUED",
    ) as exc_info:
        _fetch()

    assert "REJECTED" not in str(exc_info.value)
    assert captured[0]["context_options"] == PRODUCTION_CONTEXT_OPTIONS
    assert (
        captured[1]["context_options"]["storage_state"]["cookies"][0]["value"]
        == "new-token"
    )
    assert recordcity_fetch._cached_waf_cookies() == []


def test_stale_cached_token_gets_one_fresh_retry_in_same_fetch(monkeypatch):
    stale = _waf_cookie("stale-token")
    fresh = _waf_cookie("fresh-token")
    recordcity_fetch._remember_waf_cookies([stale])
    captured = []
    challenge_page = FakePage(
        FakeResponse(202, headers={"x-amzn-waf-action": "challenge"}),
        html=CHALLENGE_HTML,
        selector_ready=False,
    )
    product_page = FakePage(FakeResponse(200), html=PRODUCT_HTML)
    _install_browser_fake(
        monkeypatch,
        [
            (challenge_page, FakeContext([stale])),
            (product_page, FakeContext([fresh])),
        ],
        captured,
    )

    result = _fetch()

    assert result.status == 200
    assert len(captured) == 2
    assert captured[0]["context_options"]["storage_state"]["cookies"][0]["value"] == "stale-token"
    assert captured[1]["context_options"] == PRODUCTION_CONTEXT_OPTIONS
    assert recordcity_fetch._cached_waf_cookies()[0]["value"] == "fresh-token"


@pytest.mark.parametrize(
    "status, action, expected_reason",
    [
        (202, "challenge", "RC_WAF_CHALLENGE_NO_TOKEN"),
        (405, "captcha", "RC_WAF_CAPTCHA_REQUIRED"),
        (403, "", "RC_WAF_BLOCK_403"),
    ],
)
def test_waf_failures_keep_specific_operator_reason(
    monkeypatch,
    status,
    action,
    expected_reason,
):
    captured = []
    headers = {"server": "CloudFront", "x-amz-cf-id": "request-id-for-owner-log"}
    if action:
        headers["x-amzn-waf-action"] = action
    page = FakePage(
        FakeResponse(status, headers=headers),
        html=CHALLENGE_HTML if status == 202 else "Request blocked.",
        selector_ready=False,
    )
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    with pytest.raises(ScrapeBlockedError, match=expected_reason) as exc_info:
        _fetch()

    assert exc_info.value.status_code == status
    assert "WAFログ" in str(exc_info.value)


def test_existing_unaccepted_token_is_discarded_without_logging_value(monkeypatch, caplog):
    secret = "do-not-log-this-token"
    recordcity_fetch._remember_waf_cookies([_waf_cookie(secret)])
    captured = []
    page = FakePage(
        FakeResponse(202, headers={"x-amzn-waf-action": "challenge"}),
        html=CHALLENGE_HTML,
        selector_ready=False,
    )
    first_context = FakeContext([_waf_cookie(secret)])
    second_page = FakePage(
        FakeResponse(202, headers={"x-amzn-waf-action": "challenge"}),
        html=CHALLENGE_HTML,
        selector_ready=False,
    )
    second_context = FakeContext([_waf_cookie(secret)])
    _install_browser_fake(
        monkeypatch,
        [(page, first_context), (second_page, second_context)],
        captured,
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(
            ScrapeBlockedError,
            match="RC_WAF_TOKEN_PRESENT_CHALLENGE_CONTINUED",
        ) as exc_info:
            _fetch()

    assert recordcity_fetch._cached_waf_cookies() == []
    assert secret not in str(exc_info.value)
    assert secret not in caplog.text
    assert captured[0]["context_options"]["storage_state"]["cookies"][0]["value"] == secret
    assert captured[1]["context_options"] == PRODUCTION_CONTEXT_OPTIONS


def test_successful_token_is_reused_only_in_next_recordcity_context(monkeypatch):
    captured = []
    cookie = _waf_cookie("reused-secret")
    first = (FakePage(FakeResponse(200)), FakeContext([cookie]))
    second = (FakePage(FakeResponse(200)), FakeContext([cookie]))
    _install_browser_fake(monkeypatch, [first, second], captured)

    _fetch()
    _fetch()

    assert captured[0]["context_options"] == PRODUCTION_CONTEXT_OPTIONS
    storage = captured[1]["context_options"]["storage_state"]
    assert captured[1]["context_options"]["user_agent"] == NORMAL_CHROME_UA
    assert storage["origins"] == []
    assert storage["cookies"][0]["name"] == "aws-waf-token"
    assert storage["cookies"][0]["value"] == "reused-secret"


def test_wrong_site_or_page_kind_is_rejected_before_browser_start(monkeypatch):
    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("browser should not start")

    monkeypatch.setattr(recordcity_fetch, "run_browser_page_task", should_not_run)

    with pytest.raises(ValueError, match="URL種別"):
        asyncio.run(
            recordcity_fetch.fetch_recordcity_page_via_browser_pool_async(
                DETAIL_URL,
                kind="search",
                wait_selector=READY_SELECTOR,
            )
        )


def test_recordcity_fetches_are_serialized_around_token_cache(monkeypatch):
    active = 0
    max_active = 0

    async def fake_unlocked(*_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return object()

    monkeypatch.setattr(
        recordcity_fetch,
        "_fetch_recordcity_page_unlocked",
        fake_unlocked,
    )

    async def run_both():
        return await asyncio.gather(
            recordcity_fetch.fetch_recordcity_page_via_browser_pool_async(
                DETAIL_URL,
                kind="detail",
                wait_selector=READY_SELECTOR,
            ),
            recordcity_fetch.fetch_recordcity_page_via_browser_pool_async(
                DETAIL_URL,
                kind="detail",
                wait_selector=READY_SELECTOR,
            ),
        )

    results = asyncio.run(run_both())

    assert len(results) == 2
    assert max_active == 1


@pytest.mark.parametrize(
    "profile, expected_headless, expected_options",
    [
        ("patchright-current", True, PRODUCTION_CONTEXT_OPTIONS),
        (
            "patchright-headless-ua",
            True,
            {
                "locale": "ja-JP",
                "user_agent": NORMAL_CHROME_UA,
            },
        ),
        (
            "patchright-headful",
            False,
            {"locale": "ja-JP", "no_viewport": True},
        ),
        (
            "patchright-headful-tokyo",
            False,
            {
                "locale": "ja-JP",
                "no_viewport": True,
                "timezone_id": "Asia/Tokyo",
            },
        ),
    ],
)
def test_single_browser_probe_uses_isolated_profile(
    monkeypatch,
    profile,
    expected_headless,
    expected_options,
):
    captured = []
    page = FakePage(FakeResponse(200, headers={"server": "CloudFront"}))
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    result = asyncio.run(
        recordcity_fetch.probe_recordcity_browser_once_async(
            DETAIL_URL,
            profile=profile,
        )
    )

    assert result["strategy"] == profile
    assert result["target_status"] == 200
    assert result["ready_dom"] is True
    assert result["product_json_ld"] is True
    assert result["aws_waf_token"] is False
    assert result["body_bytes"] > 0
    assert len(result["body_sha256"]) == 64
    assert captured[0]["site"] == f"recordcity_probe_{profile.replace('-', '_')}"
    assert captured[0]["headless"] is expected_headless
    assert captured[0]["launch_args"] == []
    assert captured[0]["context_options"] == expected_options
    assert captured[0]["automation_backend"] == "patchright"
    assert captured[0]["channel"] == "chromium"
    assert page.selector_calls[0]["state"] == "attached"


def test_single_browser_probe_records_token_then_403_without_exposing_value(
    monkeypatch,
):
    captured = []
    challenge = FakeResponse(
        202,
        headers={
            "x-amzn-waf-action": "challenge",
            "server": "CloudFront",
            "x-amz-cf-id": "challenge-request",
        },
    )
    blocked = FakeResponse(
        403,
        headers={"server": "CloudFront", "x-amz-cf-id": "blocked-request"},
    )
    secret = "never-include-this-token"
    page = FakePage(
        challenge,
        html=CHALLENGE_HTML,
        selector_ready=False,
        wait_responses=[blocked],
        final_html="Request blocked.",
    )
    _install_browser_fake(
        monkeypatch,
        [(page, FakeContext([_waf_cookie(secret)]))],
        captured,
    )

    result = asyncio.run(
        recordcity_fetch.probe_recordcity_browser_once_async(
            DETAIL_URL,
            profile="patchright-current",
        )
    )

    assert result["target_status"] == 403
    assert result["challenge"] is True
    assert result["aws_waf_token"] is True
    assert result["ready_dom"] is False
    assert result["product_json_ld"] is False
    assert result["failure_reason"] == "RC_WAF_BLOCK_403"
    assert result["cloudfront_request_ids"] == [
        "challenge-request",
        "blocked-request",
    ]
    assert secret not in repr(result)


def test_proxy_browser_probe_passes_credentials_only_to_context(monkeypatch):
    captured = []
    secret = "proxy-password-not-for-logs"
    monkeypatch.setenv(
        "RECORDCITY_PROXY_URL",
        f"http://proxy-user:{secret}@proxy.example:8080",
    )
    page = FakePage(FakeResponse(200, headers={"server": "CloudFront"}))
    _install_browser_fake(monkeypatch, [(page, FakeContext())], captured)

    result = asyncio.run(
        recordcity_fetch.probe_recordcity_browser_once_async(
            DETAIL_URL,
            profile="patchright-headless-proxy",
        )
    )

    assert result["product_json_ld"] is True
    assert captured[0]["headless"] is True
    assert captured[0]["context_options"]["user_agent"] == NORMAL_CHROME_UA
    assert captured[0]["context_options"]["proxy"] == {
        "server": "http://proxy.example:8080",
        "username": "proxy-user",
        "password": secret,
    }
    assert secret not in repr(result)


@pytest.mark.parametrize(
    "proxy_url",
    [
        "",
        "ftp://proxy.example:21",
        "http://proxy.example/path",
        "http://proxy.example?token=secret",
    ],
)
def test_browser_proxy_options_reject_unsafe_or_unsupported_shapes(proxy_url):
    with pytest.raises(ValueError, match="RECORDCITY_PROXY_URL"):
        recordcity_fetch._browser_proxy_options(proxy_url)
