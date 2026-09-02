"""Record City-only external fetch providers and guarded page orchestration.

The shared scraping client deliberately stays unchanged. Credentials merely
make providers available to the explicit diagnostic command; production uses
one only when ``RECORDCITY_FETCH_PROVIDER`` selects it. With the default
``browser`` selection, :func:`fetch_recordcity_page_sync` delegates to the
existing Patchright adapter with the original arguments.

Provider credentials and WAF cookie values are used only for the outbound
request.  They are never included in response representations, logs, or
operator-facing exception messages.
"""
from __future__ import annotations

import base64
from collections import Counter
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote_plus, urljoin, urlparse, urlunparse

from services.html_page_adapter import HtmlPageAdapter
from services.scrape_safety import (
    ScrapeBlockedError,
    ScrapeHttpError,
    ScrapeSelectorDriftError,
    UnsafeScrapeUrlError,
    has_no_results_evidence,
    validate_fetch_response,
    validate_marketplace_url,
)


logger = logging.getLogger(__name__)

_SITE = "recordcity"
_SOURCES = ("zyte", "scraperapi", "template", "proxy")
_PRODUCTION_SOURCES = frozenset({"zyte", "scraperapi", "template"})
_PRODUCTION_PROVIDER_ENV = "RECORDCITY_FETCH_PROVIDER"
_SCRAPERAPI_ROUTING_ENV = "RECORDCITY_SCRAPERAPI_ROUTING"
_SCRAPERAPI_ROUTING_COSTS = {
    "standard": 10,
    "premium": 25,
    "ultra_premium": 75,
}
_SOURCE_ENV = {
    "zyte": "RECORDCITY_ZYTE_API_KEY",
    "scraperapi": "RECORDCITY_SCRAPERAPI_KEY",
    "template": "RECORDCITY_FETCH_API_URL_TEMPLATE",
    "proxy": "RECORDCITY_PROXY_URL",
}
_SAFE_HEADER_NAMES = frozenset(
    {"server", "x-cache", "x-amz-cf-id", "x-amzn-waf-action"}
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_EXTERNAL_RESPONSE_BYTES = 12 * 1024 * 1024
_DETAIL_PATH_RE = re.compile(r"^/(?:ja/)?catalog/(\d+)/?$")


class _ExternalBodyTooLarge(Exception):
    pass


class _BoundedBody:
    def __init__(self, limit: int = _MAX_EXTERNAL_RESPONSE_BYTES) -> None:
        self.limit = max(1, int(limit))
        self.data = bytearray()
        self.used = False
        self.tripped = False

    def __call__(self, chunk) -> None:
        self.used = True
        value = bytes(chunk or b"")
        if len(self.data) + len(value) > self.limit:
            self.tripped = True
            raise _ExternalBodyTooLarge()
        self.data.extend(value)


@dataclass(frozen=True)
class RecordCityExternalResponse:
    """A target response with only secret-safe diagnostic metadata exposed."""

    url: str
    target_status: int
    transport_status: int
    text: str = field(repr=False)
    source: str
    transport_url: str = field(default="", repr=False)
    target_headers: dict[str, str] = field(default_factory=dict, repr=False)
    header_source: str = "unknown"
    status_source: str = "unknown"
    waf_token_present: bool = False

    @property
    def status_code(self) -> int:
        return self.target_status

    @property
    def status(self) -> int:
        return self.target_status

    @property
    def headers(self) -> dict[str, str]:
        return dict(self.target_headers)

    @property
    def body(self) -> str:
        return self.text

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


def _get_curl_requests():
    # Keep the provider dependency lazy, matching the existing scraping client.
    from curl_cffi import requests

    return requests


def _configured_value(source: str) -> str:
    env_name = _SOURCE_ENV.get(source)
    if env_name is None:
        raise ValueError(f"Unsupported Record City external source: {source}")
    return str(os.environ.get(env_name, "") or "").strip()


def configured_recordcity_external_providers() -> tuple[str, ...]:
    """Return configured provider names without exposing their values."""

    return tuple(source for source in _SOURCES if _configured_value(source))


def _selected_production_provider() -> str | None:
    """Return the explicitly enabled provider; credentials alone do nothing."""

    selected = str(
        os.environ.get(_PRODUCTION_PROVIDER_ENV, "browser") or "browser"
    ).strip().lower()
    if selected in {"", "browser", "patchright"}:
        return None
    if selected not in _PRODUCTION_SOURCES:
        raise _provider_failure(
            "configuration",
            "RC_EXTERNAL_PROVIDER_CONFIG_INVALID",
        )
    if not _configured_value(selected):
        raise _provider_failure(
            selected,
            "RC_EXTERNAL_PROVIDER_NOT_CONFIGURED",
        )
    return selected


def _scraperapi_routing_params() -> dict[str, str]:
    """Return an explicit, cost-bounded ScraperAPI routing selection."""

    routing = str(os.environ.get(_SCRAPERAPI_ROUTING_ENV, "standard") or "standard")
    routing = routing.strip().lower()
    cost = _SCRAPERAPI_ROUTING_COSTS.get(routing)
    if cost is None:
        raise _provider_failure(
            "scraperapi",
            "RC_EXTERNAL_PROVIDER_CONFIG_INVALID",
        )
    params = {"max_cost": str(cost)}
    if routing in {"premium", "ultra_premium"}:
        params[routing] = "true"
    return params


def _safe_transport_origin(url: str) -> str:
    """Strip credentials, path, query, and fragment from transport diagnostics."""

    try:
        parsed = urlparse(str(url or ""))
        host = str(parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https", "socks5"} or not host:
            return ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    netloc = host if port is None else f"{host}:{port}"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def _catalog_id(url: str) -> str:
    try:
        path = str(urlparse(str(url or "")).path or "")
    except ValueError:
        return ""
    match = _DETAIL_PATH_RE.fullmatch(path)
    return str(match.group(1) or "") if match else ""


def _normalized_search_path(url: str) -> str:
    """Normalize the site's optional Japanese locale prefix for comparison."""

    try:
        path = str(urlparse(str(url or "")).path or "/")
    except ValueError:
        return ""
    if path == "/ja":
        path = "/"
    elif path.startswith("/ja/"):
        path = path[3:]
    return "/" + path.strip("/") if path.strip("/") else "/"


def _search_target_matches(requested_url: str, final_url: str) -> bool:
    """Require the rendered page to preserve the requested search identity."""

    if _normalized_search_path(requested_url) != _normalized_search_path(final_url):
        return False
    try:
        requested_query = Counter(
            parse_qsl(
                urlparse(str(requested_url or "")).query,
                keep_blank_values=True,
                max_num_fields=100,
            )
        )
        final_query = Counter(
            parse_qsl(
                urlparse(str(final_url or "")).query,
                keep_blank_values=True,
                max_num_fields=100,
            )
        )
    except (TypeError, ValueError):
        return False
    return all(final_query[item] >= count for item, count in requested_query.items())


def _validated_proxy_url(raw_url: str) -> str:
    """Validate proxy configuration without ever echoing its credentials."""

    try:
        parsed = urlparse(str(raw_url or "").strip())
        host = str(parsed.hostname or "").strip()
        parsed.port
    except (TypeError, ValueError):
        parsed = None
        host = ""
    if (
        parsed is None
        or parsed.scheme.lower() not in {"http", "https", "socks5"}
        or not host
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise _provider_failure(
            "proxy",
            "RC_EXTERNAL_PROXY_CONFIG_INVALID",
        )
    return str(raw_url).strip()


def _header_items(headers) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, list):
        items = []
        for item in headers:
            if not isinstance(item, dict):
                continue
            items.append((str(item.get("name") or ""), str(item.get("value") or "")))
        return items
    try:
        return [(str(key), str(value)) for key, value in headers.items()]
    except Exception:
        return []


def _safe_headers(headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in _header_items(headers):
        normalized = key.strip().lower()
        if normalized in _SAFE_HEADER_NAMES:
            # These values can reach operator-facing JSON and logs.  Keep the
            # small allowlist, and strip control characters so a remote
            # response cannot forge a second log line.
            safe_value = "".join(
                character
                for character in str(value or "")
                if character >= " " and character != "\x7f"
            )
            result[normalized] = safe_value.strip()[:240]
    return result


def _header_value(headers, expected_name: str) -> str:
    expected = expected_name.lower()
    for key, value in _header_items(headers):
        if key.strip().lower() == expected:
            return value.strip()
    return ""


def _has_waf_token(*, headers=None, cookies=None) -> bool:
    """Inspect cookie names only; never retain or return their values."""

    if any(
        key.strip().lower() == "set-cookie"
        and "aws-waf-token=" in value.lower()
        for key, value in _header_items(headers)
    ):
        return True
    if cookies is None:
        return False
    try:
        cookie_dict = cookies.get_dict()
    except Exception:
        try:
            cookie_dict = dict(cookies)
        except Exception:
            return False
    return any(str(name).lower() == "aws-waf-token" for name in cookie_dict)


def _provider_failure(
    source: str,
    reason: str,
    *,
    status_code: int | None = None,
    error_type: str = "",
) -> ScrapeHttpError:
    details = [f"reason={reason}", f"source={source}"]
    if status_code is not None:
        details.append(f"HTTP {status_code}")
    if error_type:
        details.append(f"error={error_type}")
    return ScrapeHttpError(
        "レコードシティの外部取得に失敗しました（" + ", ".join(details) + "）。",
        status_code=status_code,
    )


def _call_provider(source: str, operation, *, body_collector=None):
    try:
        return operation()
    except (ScrapeHttpError, UnsafeScrapeUrlError):
        raise
    except Exception as exc:
        if bool(getattr(body_collector, "tripped", False)):
            raise _provider_failure(
                source,
                "RC_EXTERNAL_RESPONSE_TOO_LARGE",
            ) from None
        # Provider exceptions can contain credential-bearing request URLs.
        raise _provider_failure(
            source,
            "RC_EXTERNAL_TRANSPORT_ERROR",
            error_type=type(exc).__name__,
        ) from None


def _bounded_provider_call(source: str, operation):
    collector = _BoundedBody()
    response = _call_provider(
        source,
        lambda: operation(collector),
        body_collector=collector,
    )
    raw_length = _header_value(getattr(response, "headers", None), "content-length")
    if raw_length:
        try:
            if int(raw_length) > _MAX_EXTERNAL_RESPONSE_BYTES:
                raise _provider_failure(
                    source,
                    "RC_EXTERNAL_RESPONSE_TOO_LARGE",
                )
        except ValueError:
            pass
    return response, collector


def _response_status(response) -> int:
    value = getattr(response, "status_code", None)
    if not isinstance(value, int) or isinstance(value, bool):
        value = getattr(response, "status", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _response_bytes(response, collector: _BoundedBody | None = None) -> bytes:
    if collector is not None and collector.used:
        return bytes(collector.data)
    value = getattr(response, "content", None)
    if value is None:
        value = getattr(response, "text", "")
    if isinstance(value, str):
        value = value.encode("utf-8")
    result = bytes(value or b"")
    if len(result) > _MAX_EXTERNAL_RESPONSE_BYTES:
        raise _provider_failure(
            "response",
            "RC_EXTERNAL_RESPONSE_TOO_LARGE",
        )
    return result


def _response_text(response, collector: _BoundedBody | None = None) -> str:
    raw = _response_bytes(response, collector)
    encoding = str(getattr(response, "encoding", "") or "utf-8")
    try:
        return raw.decode(encoding, errors="ignore")
    except LookupError:
        return raw.decode("utf-8", errors="ignore")


def _response_json(response, collector: _BoundedBody | None = None):
    if collector is not None and collector.used:
        return json.loads(_response_text(response, collector))
    # Test doubles and non-curl adapters can provide parsed JSON without
    # invoking content_callback. Keep the fallback bounded when bytes exist.
    _response_bytes(response, collector)
    return response.json()


def _metadata_status(headers, *names: str) -> int | None:
    for name in names:
        raw_value = _header_value(headers, name)
        if not raw_value:
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return 0
        return value
    return None


def _metadata_url(headers, *names: str) -> str:
    for name in names:
        value = _header_value(headers, name)
        if value:
            return value
    return ""


def _target_metadata(
    response,
    requested_url: str,
) -> tuple[str, int]:
    """Read target metadata when a transport exposes it in response headers.

    ScraperAPI documents ``sa-final-url`` and ``sa-statuscode``.  The
    RecordCity-prefixed names give an operator-provided URL-template endpoint
    an equally explicit contract without trusting the provider request URL.
    Missing metadata falls back to the already-validated requested URL and
    transport status, matching the existing Surugaya provider behaviour.
    """

    headers = getattr(response, "headers", None)
    final_url = _metadata_url(
        headers,
        "sa-final-url",
        "x-recordcity-final-url",
    ) or requested_url
    target_status = _metadata_status(
        headers,
        "sa-statuscode",
        "x-recordcity-status",
    )
    if target_status is None:
        target_status = _response_status(response)
    return final_url, target_status


def _validated_external_response(
    response: RecordCityExternalResponse,
    *,
    kind: str,
    requested_url: str | None = None,
) -> RecordCityExternalResponse:
    """Fail closed on malformed provider status or off-site final URLs."""

    if not 100 <= response.transport_status <= 599:
        raise _provider_failure(
            response.source,
            "RC_EXTERNAL_TRANSPORT_STATUS_INVALID",
        )
    if not 100 <= response.target_status <= 599:
        raise _provider_failure(
            response.source,
            "RC_EXTERNAL_TARGET_STATUS_INVALID",
        )
    final_url = validate_marketplace_url(response.url, _SITE, kind=kind)
    if kind == "detail" and requested_url:
        requested_id = _catalog_id(requested_url)
        final_id = _catalog_id(final_url)
        if not requested_id or requested_id != final_id:
            raise _provider_failure(
                response.source,
                "RC_EXTERNAL_TARGET_ID_MISMATCH",
            )
    if kind == "search" and requested_url:
        if not _search_target_matches(requested_url, final_url):
            raise _provider_failure(
                response.source,
                "RC_EXTERNAL_TARGET_SEARCH_MISMATCH",
            )
    return response


def _build_response(
    *,
    url: str,
    target_status: int,
    transport_status: int,
    text: str,
    source: str,
    transport_url: str,
    header_source: str,
    status_source: str,
    headers=None,
    cookies=None,
) -> RecordCityExternalResponse:
    return RecordCityExternalResponse(
        url=url,
        target_status=int(target_status or 0),
        transport_status=int(transport_status or 0),
        text=str(text or ""),
        source=source,
        transport_url=_safe_transport_origin(transport_url),
        target_headers=_safe_headers(headers),
        header_source=header_source,
        status_source=status_source,
        # Scraper API response cookies belong to that provider unless its API
        # explicitly exposes target-cookie metadata. Only inspect cookies from
        # a response whose headers/cookies are known to be RecordCity's.
        waf_token_present=(
            header_source == "target"
            and _has_waf_token(headers=headers, cookies=cookies)
        ),
    )


def _fetch_via_zyte(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int,
) -> RecordCityExternalResponse:
    api_key = _configured_value("zyte")
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    payload = {
        "url": url,
        "browserHtml": True,
        # AWS WAF Challenge requires its JavaScript runtime. Zyte otherwise
        # chooses whether to execute JavaScript as part of ban avoidance.
        "javascript": True,
        "geolocation": "JP",
        "httpResponseHeaders": True,
        "responseCookies": True,
        # A JSON-LD script is attached but never visible. Keep this state in
        # sync with the fixed Patchright wait contract.
        "actions": [
            {
                "action": "waitForSelector",
                "selector": {
                    "type": "css",
                    "value": wait_selector,
                    "state": "attached",
                },
            }
        ],
    }
    response, body = _bounded_provider_call(
        "zyte",
        lambda collector: _get_curl_requests().post(
            "https://api.zyte.com/v1/extract",
            headers={"Authorization": f"Basic {token}"},
            json=payload,
            timeout=timeout,
            allow_redirects=False,
            content_callback=collector,
        ),
    )
    provider_status = _response_status(response)
    if provider_status >= 400 or provider_status == 0:
        raise _provider_failure(
            "zyte",
            "RC_EXTERNAL_PROVIDER_HTTP_ERROR",
            status_code=provider_status or None,
        )
    try:
        data = _response_json(response, body)
    except Exception as exc:
        raise _provider_failure(
            "zyte",
            "RC_EXTERNAL_PROVIDER_RESPONSE_INVALID",
            error_type=type(exc).__name__,
        ) from None
    if not isinstance(data, dict):
        raise _provider_failure("zyte", "RC_EXTERNAL_PROVIDER_RESPONSE_INVALID")

    reported_url = str(data.get("url") or "").strip()
    if not reported_url:
        raise _provider_failure("zyte", "RC_EXTERNAL_PROVIDER_RESPONSE_INVALID")
    final_url = validate_marketplace_url(reported_url, _SITE, kind=kind)
    html = str(data.get("browserHtml") or "")
    if not html:
        raise _provider_failure("zyte", "RC_EXTERNAL_EMPTY_HTML")
    response_cookies = data.get("responseCookies") or []
    token_present = any(
        isinstance(cookie, dict)
        and str(cookie.get("name") or "").lower() == "aws-waf-token"
        for cookie in response_cookies
    )
    try:
        target_status = int(data.get("statusCode") or 0)
    except (TypeError, ValueError):
        raise _provider_failure(
            "zyte",
            "RC_EXTERNAL_PROVIDER_RESPONSE_INVALID",
        ) from None
    result = _build_response(
        url=final_url,
        target_status=target_status,
        transport_status=provider_status,
        text=html,
        source="zyte",
        transport_url="https://api.zyte.com",
        header_source="target",
        status_source="target",
        headers=data.get("httpResponseHeaders"),
    )
    if token_present:
        return RecordCityExternalResponse(
            url=result.url,
            target_status=result.target_status,
            transport_status=result.transport_status,
            text=result.text,
            source=result.source,
            transport_url=result.transport_url,
            target_headers=result.target_headers,
            header_source=result.header_source,
            status_source=result.status_source,
            waf_token_present=True,
        )
    return result


def _fetch_via_scraperapi(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int,
) -> RecordCityExternalResponse:
    del wait_selector
    api_key = _configured_value("scraperapi")
    routing_params = _scraperapi_routing_params()
    response, body = _bounded_provider_call(
        "scraperapi",
        lambda collector: _get_curl_requests().get(
            "https://api.scraperapi.com",
            params={
                "api_key": api_key,
                "url": url,
                "render": "true",
                "country_code": "jp",
                **routing_params,
            },
            timeout=timeout,
            allow_redirects=False,
            content_callback=collector,
        ),
    )
    provider_status = _response_status(response)
    reported_target_status = _metadata_status(
        getattr(response, "headers", None),
        "sa-statuscode",
    )
    if provider_status >= 400 and reported_target_status is None:
        raise _provider_failure(
            "scraperapi",
            "RC_EXTERNAL_PROVIDER_HTTP_ERROR",
            status_code=provider_status,
        )
    final_url, target_status = _target_metadata(
        response,
        url,
    )
    final_url = validate_marketplace_url(final_url, _SITE, kind=kind)
    return _build_response(
        url=final_url,
        target_status=target_status,
        transport_status=_response_status(response),
        text=_response_text(response, body),
        source="scraperapi",
        transport_url="https://api.scraperapi.com",
        header_source="provider",
        status_source=(
            "target_metadata" if reported_target_status is not None else "provider"
        ),
        headers=getattr(response, "headers", None),
        cookies=getattr(response, "cookies", None),
    )


def _fetch_via_template(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int,
) -> RecordCityExternalResponse:
    del wait_selector
    template = _configured_value("template")
    if "{raw_url" in template:
        raise _provider_failure(
            "template",
            "RC_EXTERNAL_TEMPLATE_CONFIG_INVALID",
        )
    fetch_url = _call_provider(
        "template",
        lambda: template.format(url=quote_plus(url)),
    )
    try:
        parsed_fetch_url = urlparse(fetch_url)
        fetch_port = parsed_fetch_url.port
    except (TypeError, ValueError):
        parsed_fetch_url = None
        fetch_port = None
    if (
        parsed_fetch_url is None
        or parsed_fetch_url.scheme.lower() != "https"
        or not parsed_fetch_url.hostname
        or parsed_fetch_url.username is not None
        or parsed_fetch_url.password is not None
        or fetch_port not in {None, 443}
        or parsed_fetch_url.fragment
    ):
        raise _provider_failure(
            "template",
            "RC_EXTERNAL_TEMPLATE_CONFIG_INVALID",
        )
    response, body = _bounded_provider_call(
        "template",
        lambda collector: _get_curl_requests().get(
            fetch_url,
            timeout=timeout,
            allow_redirects=False,
            content_callback=collector,
        ),
    )
    provider_status = _response_status(response)
    reported_target_status = _metadata_status(
        getattr(response, "headers", None),
        "x-recordcity-status",
    )
    if provider_status >= 400 and reported_target_status is None:
        raise _provider_failure(
            "template",
            "RC_EXTERNAL_PROVIDER_HTTP_ERROR",
            status_code=provider_status,
        )
    final_url, target_status = _target_metadata(
        response,
        url,
    )
    final_url = validate_marketplace_url(final_url, _SITE, kind=kind)
    return _build_response(
        url=final_url,
        target_status=target_status,
        transport_status=_response_status(response),
        text=_response_text(response, body),
        source="template",
        transport_url=fetch_url,
        header_source="provider",
        status_source=(
            "target_metadata" if reported_target_status is not None else "provider"
        ),
        headers=getattr(response, "headers", None),
        cookies=getattr(response, "cookies", None),
    )


def _fetch_via_proxy(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int,
) -> RecordCityExternalResponse:
    del wait_selector
    proxy_url = _validated_proxy_url(_configured_value("proxy"))
    current_url = validate_marketplace_url(url, _SITE, kind=kind)
    response = None
    body = None
    curl_requests = _get_curl_requests()
    session_factory = getattr(curl_requests, "Session", None)
    session = session_factory() if callable(session_factory) else curl_requests
    deadline = time.monotonic() + max(1, int(timeout))
    try:
        for redirect_count in range(6):
            remaining = max(1.0, deadline - time.monotonic())
            if time.monotonic() >= deadline:
                raise _provider_failure("proxy", "RC_EXTERNAL_TIMEOUT")
            response, body = _bounded_provider_call(
                "proxy",
                lambda collector, current_url=current_url: session.get(
                    current_url,
                    timeout=remaining,
                    impersonate="chrome120",
                    proxies={"http": proxy_url, "https": proxy_url},
                    headers={
                        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"
                    },
                    allow_redirects=False,
                    content_callback=collector,
                ),
            )
            status = _response_status(response)
            if status not in _REDIRECT_STATUSES:
                break
            location = _header_value(getattr(response, "headers", None), "location")
            if not location:
                raise _provider_failure(
                    "proxy",
                    "RC_EXTERNAL_REDIRECT_WITHOUT_LOCATION",
                    status_code=status,
                )
            if redirect_count >= 5:
                raise _provider_failure("proxy", "RC_EXTERNAL_REDIRECT_LIMIT")
            current_url = validate_marketplace_url(
                urljoin(current_url, location),
                _SITE,
                kind=kind,
            )
    finally:
        if session is not curl_requests:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    if response is None:
        raise _provider_failure("proxy", "RC_EXTERNAL_EMPTY_RESPONSE")
    response_url = str(getattr(response, "url", "") or current_url)
    final_url = validate_marketplace_url(response_url, _SITE, kind=kind)
    return _build_response(
        url=final_url,
        target_status=_response_status(response),
        transport_status=_response_status(response),
        text=_response_text(response, body),
        source="proxy",
        transport_url=proxy_url,
        header_source="target",
        status_source="target",
        headers=getattr(response, "headers", None),
        cookies=getattr(response, "cookies", None),
    )


_FETCHERS = {
    "zyte": _fetch_via_zyte,
    "scraperapi": _fetch_via_scraperapi,
    "template": _fetch_via_template,
    "proxy": _fetch_via_proxy,
}


def fetch_recordcity_external(
    url: str,
    timeout: int = 60,
    provider: str | None = None,
    *,
    kind: str | None = None,
    wait_selector: str | None = None,
) -> RecordCityExternalResponse | None:
    """Fetch through one configured provider, or return ``None`` when absent.

    Passing ``provider`` lets the diagnostic CLI test providers independently.
    Normal application fetches omit it and honor the explicit
    ``RECORDCITY_FETCH_PROVIDER`` selection. Credentials alone never change
    production behaviour or incur provider charges. The generic proxy is
    diagnostic-only and must always be requested explicitly.
    """

    if kind is None:
        try:
            normalized_url = validate_marketplace_url(url, _SITE, kind="detail")
        except UnsafeScrapeUrlError:
            kind = "search"
            normalized_url = validate_marketplace_url(url, _SITE, kind=kind)
        else:
            kind = "detail"
    else:
        normalized_url = validate_marketplace_url(url, _SITE, kind=kind)
    wait_selector = str(
        wait_selector
        or (
            "script[type='application/ld+json']"
            if kind == "detail"
            else "a[href*='/catalog/']"
        )
    )
    if provider is not None:
        normalized_source = str(provider or "").strip().lower()
        if normalized_source not in _FETCHERS:
            raise ValueError(f"Unsupported Record City external source: {provider}")
        selected = normalized_source if _configured_value(normalized_source) else None
    else:
        selected = _selected_production_provider()
    if selected is None:
        return None
    response = _FETCHERS[selected](
        normalized_url,
        kind=kind,
        wait_selector=wait_selector,
        timeout=max(1, int(timeout or 60)),
    )
    return _validated_external_response(
        response,
        kind=kind,
        requested_url=normalized_url,
    )


def _looks_like_waf_challenge(html: str) -> bool:
    normalized = str(html or "").lower()
    return (
        "window.gokuprops" in normalized
        or "awswafintegration" in normalized
        or ("token.awswaf.com" in normalized and "challenge.js" in normalized)
    )


def _looks_like_waf_captcha(html: str) -> bool:
    normalized = str(html or "").lower()
    return "awswafcaptcha" in normalized or (
        "token.awswaf.com" in normalized and "captcha.js" in normalized
    )


def _external_page_ready(
    page: HtmlPageAdapter,
    *,
    final_url: str,
    kind: str,
    wait_selector: str,
    source: str,
) -> bool:
    if kind == "detail":
        if wait_selector and not page.css(wait_selector):
            return False
        # Lazy import avoids a module cycle; recordcity_db imports this module
        # only inside its fetch function after module initialization.
        from recordcity_db import _extract_json_ld_product

        product = _extract_json_ld_product(page)
        if not product:
            return False
        expected_sku = _catalog_id(final_url)
        actual_sku = str(product.get("sku") or "").strip()
        if not expected_sku or actual_sku != expected_sku:
            raise ScrapeSelectorDriftError(
                "レコードシティの外部取得結果の商品IDが要求URLと一致しません"
                f"（reason=RC_EXTERNAL_PRODUCT_ID_MISMATCH, source={source}）。"
            )
        return True

    for node in page.css("a[href*='/catalog/']"):
        href = str(getattr(node, "attrib", {}).get("href") or "").strip()
        if not href:
            continue
        try:
            validate_marketplace_url(
                urljoin(final_url, href),
                _SITE,
                kind="detail",
            )
        except UnsafeScrapeUrlError:
            continue
        return True
    return has_no_results_evidence(page.get_text(), _SITE)


def _validate_external_page(
    response: RecordCityExternalResponse,
    *,
    kind: str,
    wait_selector: str,
) -> HtmlPageAdapter:
    final_url = validate_marketplace_url(response.url, _SITE, kind=kind)
    action = ""
    if response.header_source == "target":
        action = str(
            response.target_headers.get("x-amzn-waf-action") or ""
        ).strip().lower()
    status = response.target_status
    authoritative_status = response.status_source in {
        "target",
        "target_metadata",
    }
    html = response.text
    page = HtmlPageAdapter(html, url=final_url, status=status)

    if not authoritative_status and not 200 <= response.transport_status < 300:
        raise _provider_failure(
            response.source,
            "RC_EXTERNAL_PROVIDER_HTTP_ERROR",
            status_code=response.transport_status,
        )

    captcha_seen = (
        action == "captcha"
        or (authoritative_status and status == 405)
        or _looks_like_waf_captcha(html)
    )
    if captcha_seen:
        # Preserve provider/target attribution when the transport did not
        # expose authoritative target metadata.  A target header is explicit
        # even when the provider could not report the target status; a body
        # marker alone remains source-ambiguous as before.
        if not authoritative_status and action != "captcha":
            raise _provider_failure(
                response.source,
                "RC_EXTERNAL_BLOCK_SOURCE_AMBIGUOUS",
                status_code=response.transport_status,
            )
        # CAPTCHA is a terminal human-verification requirement.  Check it
        # before Product/listing readiness so retained JSON-LD underneath a
        # CAPTCHA page cannot be normalized into a successful HTTP 200 page.
        raise ScrapeBlockedError(
            "レコードシティの外部取得でAWS WAF CAPTCHAを検出しました"
            f"（reason=RC_EXTERNAL_WAF_CAPTCHA, source={response.source}, HTTP {status}）。",
            status_code=status,
        )

    ready = _external_page_ready(
        page,
        final_url=final_url,
        kind=kind,
        wait_selector=wait_selector,
        source=response.source,
    )

    # A verified Product/listing DOM is stronger evidence than metadata from
    # an initial WAF response retained by a browser-rendering provider.
    if ready:
        effective_status = 200 if status in {202, 403, 405} else status
        page = HtmlPageAdapter(html, url=final_url, status=effective_status)
        validate_fetch_response(page, _SITE, kind=kind)
        logger.info(
            "Record City external fetch passed: source=%s status=%s "
            "reported_status=%s token_present=%s",
            response.source,
            effective_status,
            status,
            response.waf_token_present,
        )
        return page

    if not authoritative_status and (
        _looks_like_waf_captcha(html)
        or _looks_like_waf_challenge(html)
        or "request blocked" in html.lower()
    ):
        raise _provider_failure(
            response.source,
            "RC_EXTERNAL_BLOCK_SOURCE_AMBIGUOUS",
            status_code=response.transport_status,
        )

    if (
        action == "challenge"
        or (authoritative_status and status == 202)
        or _looks_like_waf_challenge(html)
    ):
        raise ScrapeBlockedError(
            "レコードシティの外部取得で未解決のAWS WAF Challengeを検出しました"
            f"（reason=RC_EXTERNAL_WAF_CHALLENGE, source={response.source}, HTTP {status}, "
            f"token_present={response.waf_token_present}）。",
            status_code=status,
        )
    if (authoritative_status and status == 403) or "request blocked" in html.lower():
        raise ScrapeBlockedError(
            "レコードシティの外部取得が拒否されました"
            f"（reason=RC_EXTERNAL_WAF_BLOCK_403, source={response.source}, HTTP {status}）。",
            status_code=status,
        )

    # Validate the provider result directly.  HtmlPageAdapter normalizes a
    # false-y status to 200, so checking only the adapter could hide status 0.
    validate_fetch_response(response, _SITE, kind=kind, text=html)
    raise ScrapeSelectorDriftError(
        "レコードシティの外部取得結果に期待するページ要素がありません"
        f"（reason=RC_EXTERNAL_READY_SELECTOR_MISSING, source={response.source}）。"
    )


def fetch_recordcity_page_sync(
    url: str,
    *,
    kind: str,
    wait_selector: str,
    timeout: int = 45000,
    wait_selector_timeout: int = 20000,
    network_idle: bool = True,
) -> HtmlPageAdapter:
    """Use a configured external provider, otherwise preserve Patchright."""

    normalized_url = validate_marketplace_url(url, _SITE, kind=kind)
    external = fetch_recordcity_external(
        normalized_url,
        max(60, int(math.ceil(max(1, timeout) / 1000))),
        kind=kind,
        wait_selector=wait_selector,
    )
    if external is not None:
        return _validate_external_page(
            external,
            kind=kind,
            wait_selector=wait_selector,
        )

    # Lazy import prevents a circular dependency and leaves the old code path
    # byte-for-byte equivalent when no RECORDCITY_* provider is configured.
    from services.recordcity_browser_fetch import (
        fetch_recordcity_page_via_browser_pool_sync,
    )

    return fetch_recordcity_page_via_browser_pool_sync(
        normalized_url,
        kind=kind,
        network_idle=network_idle,
        timeout=timeout,
        wait_selector=wait_selector,
        wait_selector_timeout=wait_selector_timeout,
    )
