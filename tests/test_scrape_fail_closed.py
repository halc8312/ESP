from __future__ import annotations

import pytest

import mercari_db
import offmall_db
import surugaya_db
import yahoo_db
import yahuoku_db
from jobs.scrape_tasks import execute_scrape_job
from services.scrape_request import build_scrape_task_request
from services.scrape_safety import (
    ScrapeBlockedError,
    ScrapeHttpError,
    ScrapeSelectorDriftError,
    UnsafeScrapeUrlError,
    fetch_with_safe_redirects,
    install_navigation_guard,
    raise_for_blocked_navigation,
    require_search_outcome,
    validate_fetch_response,
    validate_marketplace_url,
)
from services.scraping_client import run_coro_sync
from services import scraping_client


DETAIL_URLS = {
    "mercari": "https://jp.mercari.com/item/m123",
    "yahoo": "https://store.shopping.yahoo.co.jp/shop/item.html",
    "rakuma": "https://item.fril.jp/abc123",
    "surugaya": "https://www.suruga-ya.jp/product/detail/123",
    "offmall": "https://netmall.hardoff.co.jp/product/123/",
    "yahuoku": "https://page.auctions.yahoo.co.jp/auction/x123",
    "snkrdunk": "https://snkrdunk.com/products/ABC-123",
}


class MockElement:
    def __init__(self, *, text="", href=""):
        self.text = text
        self.attrib = {"href": href} if href else {}


class MockPage:
    def __init__(self, *, css_map=None, text="", url=""):
        self._css_map = css_map or {}
        self._text = text
        self.url = url
        self.status = 200

    def css(self, selector):
        return list(self._css_map.get(selector, []))

    def find(self, _selector):
        return None

    def get_all_text(self):
        return self._text


class MockHttpResponse:
    def __init__(self, *, url, status=200, headers=None, text=""):
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.body = text


@pytest.mark.parametrize("site,url", DETAIL_URLS.items())
def test_detail_url_allowlist_accepts_each_supported_marketplace(site, url):
    assert validate_marketplace_url(url, site, kind="detail").startswith("https://")


@pytest.mark.parametrize("site,url", DETAIL_URLS.items())
def test_detail_url_allowlist_rejects_http_and_host_suffix_attacks(site, url):
    with pytest.raises(UnsafeScrapeUrlError):
        validate_marketplace_url(url.replace("https://", "http://", 1), site, kind="detail")

    host = url.split("/", 3)[2]
    with pytest.raises(UnsafeScrapeUrlError):
        validate_marketplace_url(
            url.replace(host, f"{host}.evil.example", 1),
            site,
            kind="detail",
        )


def test_fetch_response_rejects_failed_status_and_cross_domain_redirect():
    blocked = MockPage(url="https://shopping.yahoo.co.jp/search?p=test")
    blocked.status = 429
    with pytest.raises(ScrapeBlockedError, match="HTTP 429"):
        validate_fetch_response(blocked, "yahoo", kind="search")

    failed = MockPage(url="https://shopping.yahoo.co.jp/search?p=test")
    failed.status = 500
    with pytest.raises(ScrapeHttpError, match="HTTP 500"):
        validate_fetch_response(failed, "yahoo", kind="search")

    redirected = MockPage(url="https://evil.example/collect")
    with pytest.raises(UnsafeScrapeUrlError):
        validate_fetch_response(redirected, "yahoo", kind="search")


def test_search_outcome_requires_explicit_zero_results_evidence():
    require_search_outcome(
        "yahoo",
        candidate_count=0,
        text="条件に一致する検索結果がありません",
    )

    with pytest.raises(ScrapeSelectorDriftError, match="表示変更"):
        require_search_outcome(
            "yahoo",
            candidate_count=0,
            text="Yahoo!ショッピング",
        )

    with pytest.raises(ScrapeBlockedError, match="アクセス確認"):
        require_search_outcome(
            "yahoo",
            candidate_count=0,
            text="<title>Just a moment</title><script src='/cdn-cgi/challenge-platform'></script>",
        )


def test_cart_zero_count_is_not_search_zero_evidence():
    with pytest.raises(ScrapeSelectorDriftError):
        require_search_outcome(
            "yahoo",
            candidate_count=0,
            text="カート 0件 おすすめ商品",
        )


def test_static_redirect_is_validated_before_second_connection():
    start_url = "https://store.shopping.yahoo.co.jp/shop/item.html"
    calls = []

    def fetch_once(url):
        calls.append(url)
        return MockHttpResponse(
            url=url,
            status=302,
            headers={"Location": "https://169.254.169.254/latest/meta-data/"},
        )

    with pytest.raises(UnsafeScrapeUrlError):
        fetch_with_safe_redirects(
            fetch_once,
            start_url,
            "yahoo",
            kind="detail",
        )

    assert calls == [start_url]


def test_static_redirect_follows_only_validated_same_marketplace_hops():
    start_url = "https://store.shopping.yahoo.co.jp/shop/item.html"
    redirected_url = "https://store.shopping.yahoo.co.jp/shop/item-2.html"
    calls = []

    def fetch_once(url):
        calls.append(url)
        if len(calls) == 1:
            return MockHttpResponse(
                url=url,
                status=302,
                headers={"Location": "/shop/item-2.html"},
            )
        return MockHttpResponse(url=url)

    result = fetch_with_safe_redirects(
        fetch_once,
        start_url,
        "yahoo",
        kind="detail",
    )

    assert result.url == redirected_url
    assert calls == [start_url, redirected_url]


def test_browser_guard_blocks_document_but_leaves_cdn_subresource_untouched():
    captured = {}

    class Context:
        async def route(self, pattern, handler):
            captured["pattern"] = pattern
            captured["handler"] = handler

    class Request:
        def __init__(self, url, resource_type):
            self.url = url
            self.resource_type = resource_type

        def is_navigation_request(self):
            return self.resource_type == "document"

    class Route:
        def __init__(self):
            self.aborted = False
            self.continued = False

        async def abort(self, _reason):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    blocked_urls = run_coro_sync(
        install_navigation_guard(Context(), "mercari", kind="detail")
    )
    blocked_route = Route()
    run_coro_sync(
        captured["handler"](
            blocked_route,
            Request("https://169.254.169.254/latest/meta-data/", "document"),
        )
    )
    image_route = Route()
    run_coro_sync(
        captured["handler"](
            image_route,
            Request("https://static.mercdn.net/item/image.jpg", "image"),
        )
    )

    assert captured["pattern"] == "**/*"
    assert blocked_route.aborted is True
    assert blocked_route.continued is False
    assert blocked_urls == ["https://169.254.169.254/latest/meta-data/"]
    assert image_route.continued is True
    assert image_route.aborted is False


class GuardRoute:
    def __init__(self):
        self.aborted = False
        self.continued = False

    async def abort(self, _reason):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class GuardFrame:
    def __init__(self, parent=None):
        self.parent_frame = parent


class GuardRequest:
    def __init__(self, url, resource_type="document", *, frame=None):
        self.url = url
        self.resource_type = resource_type
        self.frame = frame

    def is_navigation_request(self):
        return self.resource_type == "document"


def _install_guard(site, kind):
    captured = {}

    class Context:
        async def route(self, pattern, handler):
            captured["pattern"] = pattern
            captured["handler"] = handler

    blocked_urls = run_coro_sync(install_navigation_guard(Context(), site, kind=kind))
    return blocked_urls, captured["handler"]


@pytest.mark.parametrize(
    "site, kind, frame_url",
    [
        # Mercari and Rakuma search pages both load Google Publisher Tag /
        # Tag Manager, which inserts cross-origin <iframe> documents.
        ("mercari", "search", "https://securepubads.g.doubleclick.net/gampad/ads?iu=/1/x"),
        ("mercari", "detail", "https://www.google.com/recaptcha/api2/anchor?k=abc"),
        ("rakuma", "search", "https://www.googletagmanager.com/ns.html?id=GTM-PBGNRW"),
    ],
)
def test_third_party_subframe_is_aborted_without_failing_the_scrape(site, kind, frame_url):
    """Regression: ad/tag-manager iframes must not abort a whole extraction."""
    blocked_urls, handler = _install_guard(site, kind)
    main_frame = GuardFrame()
    route = GuardRoute()

    run_coro_sync(
        handler(route, GuardRequest(frame_url, frame=GuardFrame(parent=main_frame)))
    )

    assert route.aborted is True
    assert route.continued is False
    assert blocked_urls == []
    raise_for_blocked_navigation(blocked_urls, site)


def test_same_marketplace_subframe_is_allowed_to_load():
    blocked_urls, handler = _install_guard("mercari", "detail")
    route = GuardRoute()

    run_coro_sync(
        handler(
            route,
            GuardRequest(
                "https://jp.mercari.com/embed/widget",
                frame=GuardFrame(parent=GuardFrame()),
            ),
        )
    )

    assert route.continued is True
    assert route.aborted is False
    assert blocked_urls == []


@pytest.mark.parametrize(
    "frame_url",
    [
        "https://abc123.token.awswaf.com/challenge",
        "https://regional.edge.token.awswaf.com/v1/token",
    ],
)
def test_recordcity_aws_waf_token_subframe_is_allowed(frame_url):
    blocked_urls, handler = _install_guard("recordcity", "detail")
    route = GuardRoute()

    run_coro_sync(
        handler(
            route,
            GuardRequest(
                frame_url,
                frame=GuardFrame(parent=GuardFrame()),
            ),
        )
    )

    assert route.continued is True
    assert route.aborted is False
    assert blocked_urls == []


@pytest.mark.parametrize(
    "frame_url",
    [
        "https://abc123.token.awswaf.com.evil.example/challenge",
        "https://token.awswaf.com.attacker.example/challenge",
        "http://abc123.token.awswaf.com/challenge",
        "https://abc123.token.awswaf.com:8443/challenge",
    ],
)
def test_recordcity_aws_waf_token_subframe_rejects_unsafe_url_shapes(frame_url):
    blocked_urls, handler = _install_guard("recordcity", "detail")
    route = GuardRoute()

    run_coro_sync(
        handler(
            route,
            GuardRequest(
                frame_url,
                frame=GuardFrame(parent=GuardFrame()),
            ),
        )
    )

    assert route.aborted is True
    assert route.continued is False
    assert blocked_urls == []
    raise_for_blocked_navigation(blocked_urls, "recordcity")


@pytest.mark.parametrize("site", ["mercari", "snkrdunk"])
def test_aws_waf_token_subframe_exception_is_recordcity_only(site):
    blocked_urls, handler = _install_guard(site, "detail")
    route = GuardRoute()

    run_coro_sync(
        handler(
            route,
            GuardRequest(
                "https://abc123.token.awswaf.com/challenge",
                frame=GuardFrame(parent=GuardFrame()),
            ),
        )
    )

    assert route.aborted is True
    assert route.continued is False
    assert blocked_urls == []
    raise_for_blocked_navigation(blocked_urls, site)


def test_recordcity_aws_waf_token_top_level_navigation_still_fails_closed():
    token_url = "https://abc123.token.awswaf.com/challenge"
    blocked_urls, handler = _install_guard("recordcity", "detail")
    route = GuardRoute()

    run_coro_sync(
        handler(route, GuardRequest(token_url, frame=GuardFrame()))
    )

    assert route.aborted is True
    assert route.continued is False
    assert blocked_urls == [token_url]
    with pytest.raises(UnsafeScrapeUrlError, match="許可ドメイン外"):
        raise_for_blocked_navigation(blocked_urls, "recordcity")


def test_top_level_navigation_off_domain_still_fails_closed():
    blocked_urls, handler = _install_guard("mercari", "search")
    route = GuardRoute()

    run_coro_sync(
        handler(
            route,
            GuardRequest("https://169.254.169.254/latest/meta-data/", frame=GuardFrame()),
        )
    )

    assert route.aborted is True
    assert blocked_urls == ["https://169.254.169.254/latest/meta-data/"]
    with pytest.raises(UnsafeScrapeUrlError, match="許可ドメイン外"):
        raise_for_blocked_navigation(blocked_urls, "mercari")


def test_top_level_navigation_to_wrong_page_kind_reports_off_target():
    blocked_urls, handler = _install_guard("mercari", "detail")
    route = GuardRoute()

    run_coro_sync(
        handler(route, GuardRequest("https://jp.mercari.com/", frame=GuardFrame()))
    )

    assert route.aborted is True
    assert blocked_urls == ["https://jp.mercari.com/"]
    with pytest.raises(UnsafeScrapeUrlError, match="対象外ページ"):
        raise_for_blocked_navigation(blocked_urls, "mercari")


def test_dynamic_fetch_surfaces_blocked_redirect_when_goto_aborts(monkeypatch):
    class Context:
        async def route(self, _pattern, handler):
            self.handler = handler

    class Request:
        url = "https://169.254.169.254/latest/meta-data/"
        resource_type = "document"

        def is_navigation_request(self):
            return True

    class Route:
        async def abort(self, _reason):
            return None

        async def continue_(self):
            raise AssertionError("unsafe navigation must not continue")

    class Page:
        async def goto(self, *_args, **_kwargs):
            await self.context.handler(Route(), Request())
            raise RuntimeError("navigation aborted")

    async def fake_run_browser_page_task(_site, task, **_kwargs):
        context = Context()
        page = Page()
        page.context = context
        return await task(page, context)

    monkeypatch.setattr(
        "services.browser_pool.run_browser_page_task",
        fake_run_browser_page_task,
    )

    with pytest.raises(UnsafeScrapeUrlError, match="転送"):
        scraping_client.fetch_dynamic(
            "https://jp.mercari.com/item/m123",
            network_idle=False,
        )


def test_mercari_detail_rejects_unsafe_url_before_any_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mercari_db,
        "fetch_dynamic",
        lambda url, **kwargs: calls.append((url, kwargs)),
    )

    with pytest.raises(UnsafeScrapeUrlError):
        mercari_db.scrape_item_detail(
            "https://169.254.169.254/latest/meta-data/"
        )

    assert calls == []


def test_mercari_mixed_detail_results_propagate_safety_failure(monkeypatch):
    good_url = "https://jp.mercari.com/item/m1"
    blocked_url = "https://jp.mercari.com/item/m2"

    async def fake_gather(*_args, **_kwargs):
        return [
            {"url": good_url, "title": "商品", "status": "on_sale"},
            ScrapeBlockedError("challenge"),
        ]

    monkeypatch.setattr(mercari_db, "gather_with_concurrency", fake_gather)

    with pytest.raises(ScrapeBlockedError, match="challenge"):
        run_coro_sync(
            mercari_db._collect_search_items_async(
                [good_url, blocked_url],
                max_items=1,
            )
        )


def test_mercari_partial_success_tolerates_ordinary_detail_http_failure(
    monkeypatch,
):
    good_url = "https://jp.mercari.com/item/m1"
    failed_url = "https://jp.mercari.com/item/m2"

    async def fake_gather(*_args, **_kwargs):
        return [
            {"url": good_url, "title": "商品", "status": "on_sale"},
            ScrapeHttpError("HTTP 500"),
        ]

    monkeypatch.setattr(mercari_db, "gather_with_concurrency", fake_gather)

    results = run_coro_sync(
        mercari_db._collect_search_items_async(
            [good_url, failed_url],
            max_items=1,
        )
    )

    assert [item["url"] for item in results] == [good_url]


def test_mercari_shops_200_challenge_page_fails(monkeypatch):
    url = "https://jp.mercari.com/shops/product/test-product"

    async def fake_run_browser_page_task(_site, task, **_kwargs):
        class Context:
            async def route(self, _pattern, _handler):
                return None

        class Response:
            status = 200
            headers = {}

            def __init__(self):
                self.url = url

        class Page:
            def __init__(self):
                self.url = url

            async def goto(self, *_args, **_kwargs):
                return Response()

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

            async def wait_for_selector(self, *_args, **_kwargs):
                return None

            async def wait_for_timeout(self, *_args, **_kwargs):
                return None

            async def evaluate(self, _script):
                return "Just a moment"

            async def content(self):
                return "<script src='/cdn-cgi/challenge-platform'></script>"

        return await task(Page(), Context())

    monkeypatch.setattr(
        mercari_db,
        "run_browser_page_task",
        fake_run_browser_page_task,
    )

    with pytest.raises(ScrapeBlockedError):
        run_coro_sync(mercari_db._scrape_shops_product_async(url))


def test_surugaya_explicit_zero_does_not_use_broad_web_fallback(monkeypatch):
    search_url = "https://www.suruga-ya.jp/search?search_word=no-match"

    class Response:
        status_code = 200
        status = 200
        url = search_url
        text = "<html><body>検索結果がありません</body></html>"
        content = text.encode()
        headers = {}

    monkeypatch.setattr(surugaya_db, "get_session", lambda: object())
    monkeypatch.setattr(
        surugaya_db,
        "_fetch_with_retry",
        lambda *_args, **_kwargs: (Response(), None),
    )
    monkeypatch.setattr(
        surugaya_db,
        "_search_product_urls_via_yahoo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broad fallback must not run")
        ),
    )

    assert (
        surugaya_db.scrape_search_result(
            search_url,
            max_items=3,
            max_scroll=1,
        )
        == []
    )


def test_surugaya_unsafe_redirect_fails_immediately_without_retry():
    start_url = "https://www.suruga-ya.jp/product/detail/123"

    class Response:
        status_code = 302
        status = 302
        url = start_url
        text = ""
        content = b""
        headers = {"Location": "https://169.254.169.254/latest/meta-data/"}

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.calls += 1
            return Response()

    session = Session()

    with pytest.raises(UnsafeScrapeUrlError):
        surugaya_db._fetch_with_retry(
            session,
            start_url,
            max_attempts=3,
        )

    assert session.calls == 1


def test_yahoo_search_preserves_legitimate_no_results(monkeypatch):
    page = MockPage(
        text="条件に一致する検索結果がありません",
        url="https://shopping.yahoo.co.jp/search?p=definitely-no-match",
    )
    monkeypatch.setattr("services.scraping_client.fetch_static", lambda _url: page)

    assert (
        yahoo_db.scrape_search_result(
            "https://shopping.yahoo.co.jp/search?p=definitely-no-match",
            max_items=3,
            max_scroll=1,
        )
        == []
    )


def test_yahoo_search_treats_zero_candidates_without_marker_as_selector_drift(monkeypatch):
    page = MockPage(
        text="Yahoo!ショッピング",
        url="https://shopping.yahoo.co.jp/search?p=test",
    )
    monkeypatch.setattr("services.scraping_client.fetch_static", lambda _url: page)

    with pytest.raises(ScrapeSelectorDriftError, match="表示変更"):
        yahoo_db.scrape_search_result(
            "https://shopping.yahoo.co.jp/search?p=test",
            max_items=3,
            max_scroll=1,
        )


def test_yahoo_search_never_fetches_discovered_cross_domain_detail_url(monkeypatch):
    malicious_url = "https://store.shopping.yahoo.co.jp.evil.example/shop/item.html"
    page = MockPage(
        css_map={
            "a[class*='SearchResult_SearchResultItem__detailLink']": [
                MockElement(href=malicious_url)
            ]
        },
        text="Yahoo!ショッピング",
        url="https://shopping.yahoo.co.jp/search?p=test",
    )
    monkeypatch.setattr("services.scraping_client.fetch_static", lambda _url: page)
    detail_calls = []
    monkeypatch.setattr(
        yahoo_db,
        "scrape_item_detail",
        lambda url: detail_calls.append(url) or {},
    )

    with pytest.raises(ScrapeSelectorDriftError):
        yahoo_db.scrape_search_result(
            "https://shopping.yahoo.co.jp/search?p=test",
            max_items=3,
            max_scroll=1,
        )
    assert detail_calls == []


def test_yahoo_search_fails_when_every_discovered_detail_fails(monkeypatch):
    detail_url = DETAIL_URLS["yahoo"]
    page = MockPage(
        css_map={
            "a[class*='SearchResult_SearchResultItem__detailLink']": [
                MockElement(href=detail_url)
            ]
        },
        text="検索結果 1件",
        url="https://shopping.yahoo.co.jp/search?p=test",
    )
    monkeypatch.setattr("services.scraping_client.fetch_static", lambda _url: page)
    monkeypatch.setattr(
        yahoo_db,
        "scrape_item_detail",
        lambda url: {
            "url": url,
            "title": "",
            "price": None,
            "status": "error",
        },
    )
    monkeypatch.setattr(yahoo_db, "log_scrape_result", lambda *args, **kwargs: True)

    with pytest.raises(ScrapeSelectorDriftError, match="1件も取得できません"):
        yahoo_db.scrape_search_result(
            "https://shopping.yahoo.co.jp/search?p=test",
            max_items=3,
            max_scroll=1,
        )


def test_yahoo_category_page_cannot_be_healed_into_a_detail_item(monkeypatch):
    category_url = (
        "https://store.shopping.yahoo.co.jp/homeshop/b5ae5125aaa.html"
    )
    page = MockPage(
        text="ホームショッピング カメラ・光学機器 882件 おすすめ順",
        url=category_url,
    )
    healer_calls = []

    class FakeHealer:
        def extract_with_healing(self, *_args, **_kwargs):
            healer_calls.append("text")
            return "カテゴリ内の先頭商品", False

        def extract_images_with_healing(self, *_args, **_kwargs):
            healer_calls.append("images")
            return ["https://item-shopping.c.yimg.jp/i/n/example"], False

    monkeypatch.setattr(
        "services.scraping_client.fetch_static",
        lambda *_args, **_kwargs: page,
    )
    monkeypatch.setattr(yahoo_db, "get_healer", lambda: FakeHealer())

    assert yahoo_db.scrape_item_detail_light(category_url) == {}
    assert healer_calls == []


@pytest.mark.parametrize(
    ("module", "site", "search_url", "detail_urls"),
    [
        (
            yahoo_db,
            "yahoo",
            "https://shopping.yahoo.co.jp/search?p=test",
            (
                "https://store.shopping.yahoo.co.jp/shop/failed.html",
                "https://store.shopping.yahoo.co.jp/shop/good.html",
            ),
        ),
        (
            offmall_db,
            "offmall",
            "https://netmall.hardoff.co.jp/search/?q=test",
            (
                "https://netmall.hardoff.co.jp/product/failed/",
                "https://netmall.hardoff.co.jp/product/good/",
            ),
        ),
        (
            yahuoku_db,
            "yahuoku",
            "https://auctions.yahoo.co.jp/search/search?p=test",
            (
                "https://page.auctions.yahoo.co.jp/auction/failed",
                "https://page.auctions.yahoo.co.jp/auction/good",
            ),
        ),
    ],
)
def test_sequential_search_tolerates_one_ordinary_detail_failure(
    monkeypatch,
    module,
    site,
    search_url,
    detail_urls,
):
    failed_url, good_url = detail_urls
    page = MockPage(text="検索結果 2件", url=search_url)
    monkeypatch.setattr(
        "services.scraping_client.fetch_marketplace_static",
        lambda *_args, **_kwargs: page,
    )
    monkeypatch.setattr(
        module,
        "_extract_search_urls",
        lambda *_args, **_kwargs: [failed_url, good_url],
    )
    if hasattr(module, "log_scrape_result"):
        monkeypatch.setattr(module, "log_scrape_result", lambda *_args, **_kwargs: True)

    def scrape_detail(url):
        if url == failed_url:
            raise ScrapeHttpError("HTTP 500")
        return {"url": url, "title": "商品", "status": "active"}

    monkeypatch.setattr(module, "scrape_item_detail", scrape_detail)

    results = module.scrape_search_result(
        search_url,
        max_items=1,
        max_scroll=1,
    )

    assert [item["url"] for item in results] == [good_url]


@pytest.mark.parametrize("status", ["error", "blocked", "challenge"])
def test_execute_scrape_job_does_not_erase_single_item_error_sentinel(monkeypatch, status):
    target_url = "https://jp.mercari.com/item/m123"
    monkeypatch.setattr(
        "jobs.scrape_tasks.scrape_single_item",
        lambda *_args, **_kwargs: [
            {
                "url": target_url,
                "title": "",
                "price": None,
                "status": status,
            }
        ],
    )

    request_payload = build_scrape_task_request(
        site="mercari",
        target_url=target_url,
        keyword="",
        price_min=None,
        price_max=None,
        sort="",
        category=None,
        limit=1,
        user_id=1,
        persist_to_db=False,
    )

    with pytest.raises(RuntimeError):
        execute_scrape_job(request_payload)


def test_execute_scrape_job_rejects_ambiguous_empty_single_item_result(monkeypatch):
    target_url = "https://jp.mercari.com/item/m123"
    monkeypatch.setattr(
        "jobs.scrape_tasks.scrape_single_item",
        lambda *_args, **_kwargs: [],
    )
    request_payload = build_scrape_task_request(
        site="mercari",
        target_url=target_url,
        keyword="",
        price_min=None,
        price_max=None,
        sort="",
        category=None,
        limit=1,
        user_id=1,
        persist_to_db=False,
    )

    with pytest.raises(RuntimeError, match="商品情報を取得できませんでした"):
        execute_scrape_job(request_payload)
