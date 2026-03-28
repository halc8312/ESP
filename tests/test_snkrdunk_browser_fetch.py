from services.html_page_adapter import HtmlPageAdapter
from services.snkrdunk_browser_fetch import fetch_snkrdunk_page_via_browser_pool_sync


def test_fetch_snkrdunk_page_via_browser_pool_sync_builds_html_adapter(monkeypatch):
    captured = {}

    async def fake_run_browser_page_task(site, task_coro_factory, **kwargs):
        captured["site"] = site
        captured["kwargs"] = kwargs

        class FakeResponse:
            status = 200

        class FakePage:
            url = "https://snkrdunk.com/products/test-1"

            async def goto(self, url, wait_until, timeout):
                captured["goto"] = {
                    "url": url,
                    "wait_until": wait_until,
                    "timeout": timeout,
                }
                return FakeResponse()

            async def wait_for_load_state(self, state, timeout):
                captured["wait_for_load_state"] = {
                    "state": state,
                    "timeout": timeout,
                }

            async def wait_for_selector(self, selector, timeout):
                captured["wait_for_selector"] = {
                    "selector": selector,
                    "timeout": timeout,
                }

            async def content(self):
                return """
                <html>
                  <head><title>SNKRDUNK Title</title></head>
                  <body><script id="__NEXT_DATA__" type="application/json">{}</script></body>
                </html>
                """

        await task_coro_factory(FakePage(), object())

    monkeypatch.setattr("services.snkrdunk_browser_fetch.run_browser_page_task", fake_run_browser_page_task)

    page = fetch_snkrdunk_page_via_browser_pool_sync("https://snkrdunk.com/products/test-1")

    assert captured["site"] == "snkrdunk"
    assert captured["goto"]["url"] == "https://snkrdunk.com/products/test-1"
    assert captured["wait_for_load_state"]["state"] == "networkidle"
    assert page.url == "https://snkrdunk.com/products/test-1"
    assert page.status == 200
    assert page.find("#__NEXT_DATA__") is not None


def test_scrape_item_detail_light_can_use_browser_pool_dynamic_fallback(monkeypatch):
    import snkrdunk_db

    url = "https://snkrdunk.com/products/test-1"
    monkeypatch.setenv("SNKRDUNK_USE_BROWSER_POOL_DYNAMIC", "true")
    monkeypatch.setattr("services.scraping_client.fetch_static", lambda target_url: (_ for _ in ()).throw(RuntimeError("static failed")))
    monkeypatch.setattr(
        "services.scraping_client.fetch_dynamic",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fetch_dynamic should not be used")),
    )

    sentinel_page = object()
    captured = {}

    def fake_browser_pool_fetch(target_url, network_idle=True):
        captured["call"] = (target_url, network_idle)
        return sentinel_page

    monkeypatch.setattr(
        snkrdunk_db,
        "fetch_snkrdunk_page_via_browser_pool_sync",
        fake_browser_pool_fetch,
    )
    monkeypatch.setattr(
        snkrdunk_db,
        "_parse_detail_page",
        lambda page, target_url: {
            "url": target_url,
            "title": "SNKRDUNK Item",
            "price": 22000,
            "status": "on_sale",
            "description": "",
            "image_urls": [],
            "variants": [],
        },
    )

    result = snkrdunk_db.scrape_item_detail_light(url)

    assert captured["call"] == (url, True)
    assert result["title"] == "SNKRDUNK Item"


def test_scrape_search_result_can_use_browser_pool_dynamic(monkeypatch):
    import snkrdunk_db

    monkeypatch.setenv("SNKRDUNK_USE_BROWSER_POOL_DYNAMIC", "true")
    monkeypatch.setattr(
        "services.scraping_client.fetch_dynamic",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fetch_dynamic should not be used")),
    )
    monkeypatch.setattr(snkrdunk_db, "get_selectors", lambda site, page_type, field: [])
    monkeypatch.setattr(
        snkrdunk_db,
        "fetch_snkrdunk_page_via_browser_pool_sync",
        lambda target_url, network_idle=True: HtmlPageAdapter(
            """
            <html>
              <body>
                <a href="/products/test-1">Item 1</a>
              </body>
            </html>
            """,
            url=target_url,
            status=200,
        ),
    )

    async def fake_scrape_item_detail_async(url):
        return {
            "url": url,
            "title": "SNKRDUNK Search Item",
            "price": 18000,
            "status": "on_sale",
            "description": "",
            "image_urls": [],
            "variants": [],
        }

    class DummyMetrics:
        def start(self, *args, **kwargs):
            return None

        def record_attempt(self, *args, **kwargs):
            return None

        def finish(self):
            return {}

    monkeypatch.setattr(snkrdunk_db, "_scrape_item_detail_async", fake_scrape_item_detail_async)
    monkeypatch.setattr(snkrdunk_db, "get_metrics", lambda: DummyMetrics())
    monkeypatch.setattr(snkrdunk_db, "log_scrape_result", lambda *args, **kwargs: True)
    monkeypatch.setattr(snkrdunk_db, "check_scrape_health", lambda items: {"action_required": False})

    results = snkrdunk_db.scrape_search_result(
        "https://snkrdunk.com/search?keywords=jordan",
        max_items=1,
        max_scroll=1,
    )

    assert len(results) == 1
    assert results[0]["title"] == "SNKRDUNK Search Item"
