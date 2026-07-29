import asyncio

import pytest

from services.scraping_client import (
    _safe_transport_origin,
    fetch_surugaya_external,
    gather_with_concurrency,
    get_async_fetch_settings,
    run_coro_sync,
)


@pytest.mark.asyncio
async def test_gather_with_concurrency_preserves_input_order_with_out_of_order_completion():
    async def worker(value):
        await asyncio.sleep({1: 0.03, 2: 0.01, 3: 0.02}[value])
        return value * 10

    results = await gather_with_concurrency([1, 2, 3], worker, concurrency=3)

    assert results == [10, 20, 30]


@pytest.mark.asyncio
async def test_gather_with_concurrency_returns_exceptions_and_respects_cap():
    state = {"active": 0, "max_active": 0}

    async def worker(value):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        if value == "boom":
            raise RuntimeError("expected failure")
        return value.upper()

    results = await gather_with_concurrency(["a", "boom", "c"], worker, concurrency=2)

    assert results[0] == "A"
    assert isinstance(results[1], RuntimeError)
    assert results[2] == "C"
    assert state["max_active"] <= 2


def test_get_async_fetch_settings_uses_site_defaults_and_env_override(monkeypatch):
    mercari_settings = get_async_fetch_settings("mercari")
    assert mercari_settings.concurrency == 1

    monkeypatch.setenv("RAKUMA_DETAIL_CONCURRENCY", "6")
    monkeypatch.setenv("RAKUMA_DETAIL_TIMEOUT", "25")
    monkeypatch.setenv("RAKUMA_DETAIL_RETRIES", "2")
    monkeypatch.setenv("RAKUMA_DETAIL_BACKOFF", "1.25")

    rakuma_settings = get_async_fetch_settings("rakuma")

    assert rakuma_settings.concurrency == 6
    assert rakuma_settings.timeout == 25
    assert rakuma_settings.retries == 2
    assert rakuma_settings.backoff_seconds == 1.25


def test_run_coro_sync_propagates_exceptions():
    async def boom():
        await asyncio.sleep(0)
        raise ValueError("bad coroutine")

    with pytest.raises(ValueError, match="bad coroutine"):
        run_coro_sync(boom())


def test_scraperapi_uses_https_and_never_persists_api_key(monkeypatch):
    from curl_cffi import requests as curl_requests

    for name in (
        "SURUGAYA_ZYTE_API_KEY",
        "SURUGAYA_FETCH_API_URL_TEMPLATE",
        "SURUGAYA_PROXY_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SURUGAYA_SCRAPERAPI_KEY", "top-secret-key")
    captured = {}

    class Response:
        status_code = 200
        text = "<html>ok</html>"
        url = (
            "https://api.scraperapi.com/?api_key=top-secret-key"
            "&url=https%3A%2F%2Fwww.suruga-ya.jp"
        )

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr(curl_requests, "get", fake_get)
    target = "https://www.suruga-ya.jp/product/detail/123"

    result = fetch_surugaya_external(target)

    assert captured["url"] == "https://api.scraperapi.com"
    assert captured["kwargs"]["allow_redirects"] is False
    assert result.url == target
    assert result.transport_url == "https://api.scraperapi.com"
    assert "top-secret-key" not in result.transport_url


def test_scraperapi_transport_error_does_not_expose_api_key(monkeypatch):
    from curl_cffi import requests as curl_requests

    for name in (
        "SURUGAYA_ZYTE_API_KEY",
        "SURUGAYA_FETCH_API_URL_TEMPLATE",
        "SURUGAYA_PROXY_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SURUGAYA_SCRAPERAPI_KEY", "top-secret-key")
    monkeypatch.setattr(
        curl_requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "request failed: https://api.scraperapi.com"
                "?api_key=top-secret-key"
            )
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        fetch_surugaya_external(
            "https://www.suruga-ya.jp/product/detail/123"
        )

    assert "scraperapi" in str(exc_info.value)
    assert "top-secret-key" not in str(exc_info.value)


def test_transport_origin_strips_credentials_path_and_query():
    origin = _safe_transport_origin(
        "https://user:secret@fetch.example:8443/api/key-123"
        "?token=top-secret"
    )

    assert origin == "https://fetch.example:8443"
    assert "secret" not in origin
    assert "token" not in origin


def test_template_transport_diagnostic_is_secret_free(monkeypatch):
    from curl_cffi import requests as curl_requests

    monkeypatch.delenv("SURUGAYA_ZYTE_API_KEY", raising=False)
    monkeypatch.delenv("SURUGAYA_SCRAPERAPI_KEY", raising=False)
    monkeypatch.delenv("SURUGAYA_PROXY_URL", raising=False)
    monkeypatch.setenv(
        "SURUGAYA_FETCH_API_URL_TEMPLATE",
        "https://user:secret@fetch.example/api?token=hidden&target={url}",
    )
    target = "https://www.suruga-ya.jp/product/detail/123"

    class Response:
        status_code = 200
        text = "<html>ok</html>"
        url = "https://fetch.example/api?token=hidden"

    monkeypatch.setattr(curl_requests, "get", lambda *_args, **_kwargs: Response())

    result = fetch_surugaya_external(target)

    assert result.url == target
    assert result.transport_url == "https://fetch.example"
    assert "secret" not in result.transport_url
    assert "hidden" not in result.transport_url


def test_proxy_transport_diagnostic_uses_sanitized_proxy_origin(monkeypatch):
    from curl_cffi import requests as curl_requests

    monkeypatch.delenv("SURUGAYA_ZYTE_API_KEY", raising=False)
    monkeypatch.delenv("SURUGAYA_SCRAPERAPI_KEY", raising=False)
    monkeypatch.delenv("SURUGAYA_FETCH_API_URL_TEMPLATE", raising=False)
    monkeypatch.setenv(
        "SURUGAYA_PROXY_URL",
        "http://user:secret@proxy.example:8080/path?token=hidden",
    )
    target = "https://www.suruga-ya.jp/product/detail/123"

    class Response:
        status_code = 200
        status = 200
        text = "<html>ok</html>"
        body = text
        content = text.encode()
        headers = {}
        url = target

    monkeypatch.setattr(curl_requests, "get", lambda *_args, **_kwargs: Response())

    result = fetch_surugaya_external(target)

    assert result.url == target
    assert result.transport_url == "http://proxy.example:8080"
    assert "secret" not in result.transport_url
    assert "hidden" not in result.transport_url
