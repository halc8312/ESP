import json

import pytest

from services import recordcity_external_fetch as external_fetch
from services.scrape_safety import (
    ScrapeBlockedError,
    ScrapeHttpError,
    ScrapeSelectorDriftError,
    UnsafeScrapeUrlError,
)


DETAIL_URL = "https://www.recordcity.jp/catalog/4936480"
SEARCH_URL = "https://www.recordcity.jp/catalog?narrow_down_3=3"
READY_SELECTOR = "script[type='application/ld+json']"
PRODUCT_HTML = """<html><body>
<script type="application/ld+json">{"@type":"Product","name":"Sunrise","sku":"4936480"}</script>
</body></html>"""
CHALLENGE_HTML = """<html><script>
window.gokuProps = {};
AwsWafIntegration.fetch('https://example.token.awswaf.com/challenge.js');
</script></html>"""


class FakeCookies:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get_dict(self):
        return dict(self._values)


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        *,
        text=PRODUCT_HTML,
        url=DETAIL_URL,
        headers=None,
        cookies=None,
        payload=None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = dict(headers or {})
        self.cookies = cookies or FakeCookies()
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


@pytest.fixture(autouse=True)
def clear_recordcity_external_env(monkeypatch):
    for env_name in (
        "RECORDCITY_ZYTE_API_KEY",
        "RECORDCITY_SCRAPERAPI_KEY",
        "RECORDCITY_SCRAPERAPI_ROUTING",
        "RECORDCITY_FETCH_API_URL_TEMPLATE",
        "RECORDCITY_PROXY_URL",
        "RECORDCITY_FETCH_PROVIDER",
    ):
        monkeypatch.delenv(env_name, raising=False)


def _response(
    *,
    status=200,
    html=PRODUCT_HTML,
    source="test",
    url=DETAIL_URL,
    headers=None,
    token=False,
):
    return external_fetch.RecordCityExternalResponse(
        url=url,
        target_status=status,
        transport_status=status,
        text=html,
        source=source,
        transport_url="https://provider.example",
        target_headers=headers or {},
        header_source="target",
        status_source="target",
        waf_token_present=token,
    )


def test_unconfigured_orchestrator_preserves_patchright_call(monkeypatch):
    captured = {}
    expected = object()

    def fake_browser(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(
        "services.recordcity_browser_fetch.fetch_recordcity_page_via_browser_pool_sync",
        fake_browser,
    )

    result = external_fetch.fetch_recordcity_page_sync(
        DETAIL_URL,
        kind="detail",
        wait_selector=READY_SELECTOR,
        timeout=45000,
        wait_selector_timeout=20000,
        network_idle=True,
    )

    assert result is expected
    assert captured == {
        "url": DETAIL_URL,
        "kind": "detail",
        "network_idle": True,
        "timeout": 45000,
        "wait_selector": READY_SELECTOR,
        "wait_selector_timeout": 20000,
    }


def test_zyte_uses_browser_html_japan_and_attached_selector(monkeypatch):
    secret = "zyte-top-secret"
    monkeypatch.setenv("RECORDCITY_ZYTE_API_KEY", secret)
    captured = {}
    payload = {
        "url": DETAIL_URL,
        "statusCode": 200,
        "browserHtml": PRODUCT_HTML,
        "httpResponseHeaders": [
            {"name": "Server", "value": "CloudFront"},
            {"name": "Set-Cookie", "value": "private=value"},
        ],
        "responseCookies": [
            {"name": "aws-waf-token", "value": "do-not-expose"},
        ],
    }

    class Client:
        @staticmethod
        def post(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse(payload=payload, url="https://api.zyte.com/v1/extract")

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="zyte",
        kind="detail",
        wait_selector=READY_SELECTOR,
    )

    assert result is not None
    assert result.target_status == 200
    assert result.transport_status == 200
    assert result.target_headers == {"server": "CloudFront"}
    assert result.waf_token_present is True
    assert result.source == "zyte"
    assert secret not in repr(result)
    assert "do-not-expose" not in repr(result)
    assert captured["url"] == "https://api.zyte.com/v1/extract"
    request_payload = captured["kwargs"]["json"]
    assert request_payload["browserHtml"] is True
    assert request_payload["javascript"] is True
    assert request_payload["geolocation"] == "JP"
    assert request_payload["httpResponseHeaders"] is True
    assert request_payload["responseCookies"] is True
    assert request_payload["actions"] == [
        {
            "action": "waitForSelector",
            "selector": {
                "type": "css",
                "value": READY_SELECTOR,
                "state": "attached",
            },
        }
    ]
    assert captured["kwargs"]["allow_redirects"] is False


def test_zyte_final_url_is_validated_before_result(monkeypatch):
    monkeypatch.setenv("RECORDCITY_ZYTE_API_KEY", "secret")

    class Client:
        @staticmethod
        def post(*_args, **_kwargs):
            return FakeResponse(
                payload={
                    "url": "https://recordcity.jp.evil.example/catalog/4936480",
                    "statusCode": 200,
                    "browserHtml": PRODUCT_HTML,
                }
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(UnsafeScrapeUrlError):
        external_fetch.fetch_recordcity_external(DETAIL_URL, provider="zyte")


def test_zyte_reads_json_from_curl_content_callback(monkeypatch):
    monkeypatch.setenv("RECORDCITY_ZYTE_API_KEY", "secret")
    payload = {
        "url": DETAIL_URL,
        "statusCode": 200,
        "browserHtml": PRODUCT_HTML,
    }

    class Client:
        @staticmethod
        def post(*_args, **kwargs):
            kwargs["content_callback"](json.dumps(payload).encode("utf-8"))
            return FakeResponse(text="", payload=None)

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="zyte",
    )

    assert result is not None
    assert result.text == PRODUCT_HTML


def test_scraperapi_request_and_external_orchestrator(monkeypatch):
    secret = "scraperapi-top-secret"
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", secret)
    monkeypatch.setenv("RECORDCITY_FETCH_PROVIDER", "scraperapi")
    captured = {}

    class Client:
        @staticmethod
        def get(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse(
                headers={
                    "X-Amz-Cf-Id": "request-id",
                    "Set-Cookie": "aws-waf-token=do-not-expose",
                },
            )

    async_browser_called = False

    def should_not_use_browser(*_args, **_kwargs):
        nonlocal async_browser_called
        async_browser_called = True
        raise AssertionError("browser path must not run")

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)
    monkeypatch.setattr(
        "services.recordcity_browser_fetch.fetch_recordcity_page_via_browser_pool_sync",
        should_not_use_browser,
    )

    page = external_fetch.fetch_recordcity_page_sync(
        DETAIL_URL,
        kind="detail",
        wait_selector=READY_SELECTOR,
    )

    assert "Product" in page.body
    assert async_browser_called is False
    assert captured["url"] == "https://api.scraperapi.com"
    assert captured["kwargs"]["params"] == {
        "api_key": secret,
        "url": DETAIL_URL,
        "render": "true",
        "country_code": "jp",
        "max_cost": "10",
    }
    assert captured["kwargs"]["allow_redirects"] is False


def test_scraperapi_provider_cookie_is_not_reported_as_recordcity_token(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse(
                cookies=FakeCookies({"aws-waf-token": "provider-cookie"}),
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="scraperapi",
    )

    assert result is not None
    assert result.header_source == "provider"
    assert result.waf_token_present is False


def test_scraperapi_reads_html_from_curl_content_callback(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **kwargs):
            kwargs["content_callback"](PRODUCT_HTML.encode("utf-8"))
            return FakeResponse(text="")

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="scraperapi",
    )

    assert result is not None
    assert result.text == PRODUCT_HTML


def test_scraperapi_uses_reported_target_status_and_validates_final_url(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse(
                status_code=200,
                url="https://api.scraperapi.com",
                headers={
                    "sa-statuscode": "202",
                    "sa-final-url": "https://www.recordcity.jp/ja/catalog/4936480",
                    "X-Amzn-Waf-Action": "challenge",
                },
                text=CHALLENGE_HTML,
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="scraperapi",
    )

    assert result is not None
    assert result.transport_status == 200
    assert result.target_status == 202
    assert result.url == "https://www.recordcity.jp/ja/catalog/4936480"
    assert result.target_headers == {"x-amzn-waf-action": "challenge"}
    assert result.header_source == "provider"
    assert result.status_source == "target_metadata"


@pytest.mark.parametrize(
    "routing, expected_flag, expected_cost",
    [
        ("premium", "premium", "25"),
        ("ultra_premium", "ultra_premium", "75"),
    ],
)
def test_scraperapi_paid_routing_is_explicit_and_cost_bounded(
    monkeypatch,
    routing,
    expected_flag,
    expected_cost,
):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_ROUTING", routing)
    captured = {}

    class Client:
        @staticmethod
        def get(*_args, **kwargs):
            captured.update(kwargs["params"])
            return FakeResponse()

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="scraperapi",
    )

    assert result is not None
    assert captured[expected_flag] == "true"
    assert captured["max_cost"] == expected_cost


def test_scraperapi_rejects_unknown_routing_before_network(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_ROUTING", "unlimited")
    monkeypatch.setattr(
        external_fetch,
        "_get_curl_requests",
        lambda: (_ for _ in ()).throw(AssertionError("network must not run")),
    )

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_PROVIDER_CONFIG_INVALID",
    ):
        external_fetch.fetch_recordcity_external(
            DETAIL_URL,
            provider="scraperapi",
        )


def test_scraperapi_rejects_reported_offsite_final_url(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse(
                headers={
                    "sa-statuscode": "200",
                    "sa-final-url": "https://evil.example/catalog/4936480",
                }
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(UnsafeScrapeUrlError):
        external_fetch.fetch_recordcity_external(
            DETAIL_URL,
            provider="scraperapi",
        )


def test_template_transport_diagnostic_strips_credentials_path_and_query(monkeypatch):
    monkeypatch.setenv(
        "RECORDCITY_FETCH_API_URL_TEMPLATE",
        "https://fetch.example/api?token=hidden&target={url}",
    )

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(DETAIL_URL, provider="template")

    assert result is not None
    assert result.transport_url == "https://fetch.example"
    assert "hidden" not in result.transport_url


def test_provider_exception_does_not_expose_template_or_credentials(monkeypatch):
    secret = "template-secret-value"
    monkeypatch.setenv(
        "RECORDCITY_FETCH_API_URL_TEMPLATE",
        f"https://fetch.example/api?token={secret}&target={{url}}",
    )

    class Client:
        @staticmethod
        def get(url, **_kwargs):
            raise RuntimeError(f"failed requesting {url}")

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(ScrapeHttpError) as exc_info:
        external_fetch.fetch_recordcity_external(DETAIL_URL, provider="template")

    message = str(exc_info.value)
    assert "RC_EXTERNAL_TRANSPORT_ERROR" in message
    assert secret not in message
    assert "fetch.example" not in message


def test_proxy_follows_only_validated_recordcity_redirects(monkeypatch):
    proxy_secret = "proxy-password"
    proxy_url = f"http://user:{proxy_secret}@proxy.example:8080"
    monkeypatch.setenv("RECORDCITY_PROXY_URL", proxy_url)
    calls = []

    class Client:
        @staticmethod
        def get(url, **kwargs):
            calls.append((url, kwargs))
            if len(calls) == 1:
                return FakeResponse(
                    302,
                    url=url,
                    headers={"Location": "/ja/catalog/4936480"},
                )
            return FakeResponse(200, url=url)

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(DETAIL_URL, provider="proxy")

    assert result is not None
    assert result.url == "https://www.recordcity.jp/ja/catalog/4936480"
    assert result.transport_url == "http://proxy.example:8080"
    assert proxy_secret not in result.transport_url
    assert len(calls) == 2
    assert calls[0][1]["impersonate"] == "chrome120"
    assert calls[0][1]["allow_redirects"] is False


def test_proxy_rejects_invalid_configuration_without_exposing_secret(monkeypatch):
    secret = "proxy-secret"
    monkeypatch.setenv(
        "RECORDCITY_PROXY_URL",
        f"http://user:{secret}@proxy.example:8080/path?token=hidden",
    )

    with pytest.raises(ScrapeHttpError, match="RC_EXTERNAL_PROXY_CONFIG_INVALID") as exc_info:
        external_fetch.fetch_recordcity_external(DETAIL_URL, provider="proxy")

    assert secret not in str(exc_info.value)
    assert "hidden" not in str(exc_info.value)


def test_proxy_rejects_cross_origin_redirect_without_following(monkeypatch):
    monkeypatch.setenv("RECORDCITY_PROXY_URL", "http://proxy.example:8080")
    calls = []

    class Client:
        @staticmethod
        def get(url, **_kwargs):
            calls.append(url)
            return FakeResponse(
                302,
                url=url,
                headers={"Location": "https://evil.example/catalog/4936480"},
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(UnsafeScrapeUrlError):
        external_fetch.fetch_recordcity_external(DETAIL_URL, provider="proxy")

    assert calls == [DETAIL_URL]


def test_proxy_configuration_is_not_selected_by_production_orchestrator(monkeypatch):
    monkeypatch.setenv(
        "RECORDCITY_PROXY_URL",
        "http://user:secret@proxy.example:8080",
    )
    captured = {}
    expected = object()

    def fake_browser(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(
        "services.recordcity_browser_fetch.fetch_recordcity_page_via_browser_pool_sync",
        fake_browser,
    )
    monkeypatch.setattr(
        external_fetch,
        "_get_curl_requests",
        lambda: (_ for _ in ()).throw(
            AssertionError("diagnostic proxy transport must not run")
        ),
    )

    result = external_fetch.fetch_recordcity_page_sync(
        DETAIL_URL,
        kind="detail",
        wait_selector=READY_SELECTOR,
    )

    assert result is expected
    assert captured["url"] == DETAIL_URL


@pytest.mark.parametrize(
    "response,reason",
    [
        (
            _response(
                status=202,
                html=CHALLENGE_HTML,
                headers={"x-amzn-waf-action": "challenge"},
            ),
            "RC_EXTERNAL_WAF_CHALLENGE",
        ),
        (
            _response(status=405, html="captcha", headers={"x-amzn-waf-action": "captcha"}),
            "RC_EXTERNAL_WAF_CAPTCHA",
        ),
        (_response(status=403, html="Request blocked."), "RC_EXTERNAL_WAF_BLOCK_403"),
        (_response(status=200, html="Request blocked"), "RC_EXTERNAL_WAF_BLOCK_403"),
    ],
)
def test_external_waf_responses_keep_specific_failure_reason(response, reason):
    with pytest.raises(ScrapeBlockedError, match=reason):
        external_fetch._validate_external_page(
            response,
            kind="detail",
            wait_selector=READY_SELECTOR,
        )


def test_external_result_requires_ready_selector():
    with pytest.raises(
        ScrapeSelectorDriftError,
        match="RC_EXTERNAL_READY_SELECTOR_MISSING",
    ):
        external_fetch._validate_external_page(
            _response(html="<html><body>ordinary page</body></html>"),
            kind="detail",
            wait_selector=READY_SELECTOR,
        )


def test_external_provider_configuration_reports_names_not_values(monkeypatch):
    monkeypatch.setenv("RECORDCITY_ZYTE_API_KEY", "secret-one")
    monkeypatch.setenv("RECORDCITY_PROXY_URL", "http://user:secret-two@proxy.example")

    configured = external_fetch.configured_recordcity_external_providers()

    assert configured == ("zyte", "proxy")
    assert "secret-one" not in repr(configured)
    assert "secret-two" not in repr(configured)


def test_unconfigured_explicit_provider_does_not_make_network_request(monkeypatch):
    monkeypatch.setattr(
        external_fetch,
        "_get_curl_requests",
        lambda: (_ for _ in ()).throw(AssertionError("network must not run")),
    )

    assert external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="zyte",
    ) is None


def test_external_fetch_validates_target_before_provider_selection():
    with pytest.raises(UnsafeScrapeUrlError):
        external_fetch.fetch_recordcity_external(
            "https://recordcity.jp.evil.example/catalog/4936480",
            provider="zyte",
        )

    assert (
        external_fetch.fetch_recordcity_external(DETAIL_URL, provider="zyte")
        is None
    )


@pytest.mark.parametrize("field", ["target_status", "transport_status"])
def test_external_response_rejects_invalid_status_metadata(field):
    response = _response()
    values = {
        "url": response.url,
        "target_status": response.target_status,
        "transport_status": response.transport_status,
        "text": response.text,
        "source": response.source,
        "transport_url": response.transport_url,
        "target_headers": response.target_headers,
        "header_source": response.header_source,
        "status_source": response.status_source,
        "waf_token_present": response.waf_token_present,
    }
    values[field] = 0

    with pytest.raises(ScrapeHttpError, match=field.upper()):
        external_fetch._validated_external_response(
            external_fetch.RecordCityExternalResponse(**values),
            kind="detail",
        )


def test_credentials_do_not_enable_paid_provider_without_explicit_selection(
    monkeypatch,
):
    monkeypatch.setenv("RECORDCITY_ZYTE_API_KEY", "diagnostic-only-secret")
    expected = object()
    monkeypatch.setattr(
        "services.recordcity_browser_fetch.fetch_recordcity_page_via_browser_pool_sync",
        lambda *_args, **_kwargs: expected,
    )
    monkeypatch.setattr(
        external_fetch,
        "_get_curl_requests",
        lambda: (_ for _ in ()).throw(AssertionError("paid provider must not run")),
    )

    result = external_fetch.fetch_recordcity_page_sync(
        DETAIL_URL,
        kind="detail",
        wait_selector=READY_SELECTOR,
    )

    assert result is expected


def test_selected_provider_requires_matching_credentials(monkeypatch):
    monkeypatch.setenv("RECORDCITY_FETCH_PROVIDER", "zyte")

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_PROVIDER_NOT_CONFIGURED",
    ):
        external_fetch.fetch_recordcity_page_sync(
            DETAIL_URL,
            kind="detail",
            wait_selector=READY_SELECTOR,
        )


def test_scraperapi_provider_403_is_not_attributed_to_recordcity(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse(status_code=403, text="invalid api key")

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_PROVIDER_HTTP_ERROR",
    ):
        external_fetch.fetch_recordcity_external(
            DETAIL_URL,
            provider="scraperapi",
        )


def test_scraperapi_reported_target_403_remains_target_evidence(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse(
                status_code=403,
                text="Request blocked.",
                headers={"sa-statuscode": "403"},
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    result = external_fetch.fetch_recordcity_external(
        DETAIL_URL,
        provider="scraperapi",
    )

    assert result is not None
    assert result.transport_status == 403
    assert result.target_status == 403
    assert result.status_source == "target_metadata"


def test_provider_202_without_target_metadata_is_not_called_recordcity_waf():
    response = external_fetch.RecordCityExternalResponse(
        url=DETAIL_URL,
        target_status=202,
        transport_status=202,
        text="<html><body>provider job accepted</body></html>",
        source="test",
        status_source="provider",
    )

    with pytest.raises(ScrapeSelectorDriftError) as exc_info:
        external_fetch._validate_external_page(
            response,
            kind="detail",
            wait_selector=READY_SELECTOR,
        )

    assert "RC_EXTERNAL_WAF" not in str(exc_info.value)


def test_provider_redirect_without_target_metadata_is_provider_error():
    response = external_fetch.RecordCityExternalResponse(
        url=DETAIL_URL,
        target_status=302,
        transport_status=302,
        text="<html><body>redirect</body></html>",
        source="test",
        status_source="provider",
    )

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_PROVIDER_HTTP_ERROR",
    ):
        external_fetch._validate_external_page(
            response,
            kind="detail",
            wait_selector=READY_SELECTOR,
        )


def test_provider_waf_body_without_target_metadata_is_ambiguous():
    response = external_fetch.RecordCityExternalResponse(
        url=DETAIL_URL,
        target_status=200,
        transport_status=200,
        text=CHALLENGE_HTML,
        source="test",
        status_source="provider",
    )

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_BLOCK_SOURCE_AMBIGUOUS",
    ) as exc_info:
        external_fetch._validate_external_page(
            response,
            kind="detail",
            wait_selector=READY_SELECTOR,
        )

    assert "RC_EXTERNAL_WAF" not in str(exc_info.value)


def test_external_search_rejects_final_url_that_drops_requested_query(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse(
                headers={
                    "sa-statuscode": "200",
                    "sa-final-url": "https://www.recordcity.jp/ja/catalog",
                }
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_TARGET_SEARCH_MISMATCH",
    ):
        external_fetch.fetch_recordcity_external(
            SEARCH_URL,
            provider="scraperapi",
            kind="search",
        )


def test_external_response_rejects_wrong_final_catalog_id(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse(
                headers={
                    "sa-statuscode": "200",
                    "sa-final-url": "https://www.recordcity.jp/catalog/999",
                }
            )

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(ScrapeHttpError, match="RC_EXTERNAL_TARGET_ID_MISMATCH"):
        external_fetch.fetch_recordcity_external(
            DETAIL_URL,
            provider="scraperapi",
        )


def test_external_product_sku_must_match_catalog_id():
    wrong_product = PRODUCT_HTML.replace('"4936480"', '"999"')

    with pytest.raises(
        ScrapeSelectorDriftError,
        match="RC_EXTERNAL_PRODUCT_ID_MISMATCH",
    ):
        external_fetch._validate_external_page(
            _response(html=wrong_product),
            kind="detail",
            wait_selector=READY_SELECTOR,
        )


def test_verified_product_dom_wins_over_initial_challenge_metadata():
    page = external_fetch._validate_external_page(
        _response(
            status=202,
            html=PRODUCT_HTML,
            headers={"x-amzn-waf-action": "challenge"},
        ),
        kind="detail",
        wait_selector=READY_SELECTOR,
    )

    assert page.status == 200


def test_external_response_body_is_bounded(monkeypatch):
    monkeypatch.setenv("RECORDCITY_SCRAPERAPI_KEY", "secret")

    class Client:
        @staticmethod
        def get(*_args, **kwargs):
            kwargs["content_callback"](
                b"x" * (external_fetch._MAX_EXTERNAL_RESPONSE_BYTES + 1)
            )
            raise AssertionError("body callback should stop the transfer")

    monkeypatch.setattr(external_fetch, "_get_curl_requests", lambda: Client)

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_RESPONSE_TOO_LARGE",
    ):
        external_fetch.fetch_recordcity_external(
            DETAIL_URL,
            provider="scraperapi",
        )


def test_template_rejects_unencoded_raw_url_placeholder(monkeypatch):
    monkeypatch.setenv(
        "RECORDCITY_FETCH_API_URL_TEMPLATE",
        "https://fetch.example/api?target={raw_url}",
    )

    with pytest.raises(
        ScrapeHttpError,
        match="RC_EXTERNAL_TEMPLATE_CONFIG_INVALID",
    ):
        external_fetch.fetch_recordcity_external(
            DETAIL_URL,
            provider="template",
        )


def test_external_search_accepts_explicit_no_results_page():
    response = external_fetch.RecordCityExternalResponse(
        url="https://www.recordcity.jp/catalog?narrow_down_3=3",
        target_status=200,
        transport_status=200,
        text="<html><body>該当する商品がありません</body></html>",
        source="test",
    )

    page = external_fetch._validate_external_page(
        response,
        kind="search",
        wait_selector="a[href*='/catalog/']",
    )

    assert page.status == 200


def test_external_search_accepts_valid_catalog_link():
    response = external_fetch.RecordCityExternalResponse(
        url="https://www.recordcity.jp/catalog?narrow_down_3=3",
        target_status=200,
        transport_status=200,
        text='<html><body><a href="/catalog/4936480">Sunrise</a></body></html>',
        source="test",
    )

    page = external_fetch._validate_external_page(
        response,
        kind="search",
        wait_selector="a[href*='/catalog/']",
    )

    assert page.status == 200


def test_external_search_rejects_only_offsite_catalog_link():
    response = external_fetch.RecordCityExternalResponse(
        url="https://www.recordcity.jp/catalog?narrow_down_3=3",
        target_status=200,
        transport_status=200,
        text=(
            '<html><body><a href="https://evil.example/catalog/4936480">'
            "Sunrise</a></body></html>"
        ),
        source="test",
    )

    with pytest.raises(
        ScrapeSelectorDriftError,
        match="RC_EXTERNAL_READY_SELECTOR_MISSING",
    ):
        external_fetch._validate_external_page(
            response,
            kind="search",
            wait_selector="a[href*='/catalog/']",
        )
