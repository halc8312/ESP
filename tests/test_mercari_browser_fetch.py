from services.mercari_browser_fetch import (
    fetch_mercari_page_and_payloads_via_browser_pool_sync,
    fetch_mercari_page_via_browser_pool_sync,
)


def test_fetch_mercari_page_via_browser_pool_sync_builds_html_adapter(monkeypatch):
    captured = {}

    async def fake_run_browser_page_task(site, task_coro_factory, **kwargs):
        captured["site"] = site
        captured["kwargs"] = kwargs

        class FakeResponse:
            status = 200

        class FakePage:
            url = "https://jp.mercari.com/item/m123"

            def on(self, event, handler):
                pass

            async def goto(self, url, wait_until, timeout):
                captured["goto"] = {
                    "url": url,
                    "wait_until": wait_until,
                    "timeout": timeout,
                }
                return FakeResponse()

            async def wait_for_load_state(self, state, timeout):
                captured.setdefault("wait_for_load_state_calls", []).append(
                    {"state": state, "timeout": timeout}
                )
                captured["wait_for_load_state"] = {
                    "state": state,
                    "timeout": timeout,
                }

            async def wait_for_selector(self, selector, timeout):
                captured["wait_for_selector"] = {
                    "selector": selector,
                    "timeout": timeout,
                }

            async def wait_for_response(self, predicate, timeout):
                captured.setdefault("wait_for_response_calls", []).append(
                    {"timeout": timeout}
                )

            async def query_selector_all(self, selector):
                return []

            async def query_selector(self, selector):
                return None

            async def wait_for_timeout(self, ms):
                pass

            async def content(self):
                return """
                <html>
                  <head><title>Mercari Title</title></head>
                  <body><h1>Mercari Title</h1></body>
                </html>
                """

        await task_coro_factory(FakePage(), object())

    monkeypatch.setattr("services.mercari_browser_fetch.run_browser_page_task", fake_run_browser_page_task)

    page = fetch_mercari_page_via_browser_pool_sync("https://jp.mercari.com/item/m123")

    assert captured["site"] == "mercari"
    assert captured["goto"]["url"] == "https://jp.mercari.com/item/m123"
    assert captured["wait_for_load_state"]["state"] == "networkidle"
    assert captured["wait_for_selector"]["selector"] == "h1, [data-testid='price']"
    assert page.url == "https://jp.mercari.com/item/m123"
    assert page.status == 200
    assert page.css("h1")[0].text == "Mercari Title"


def test_fetch_mercari_page_and_payloads_waits_for_target_items_get(monkeypatch):
    """Regression: mercari `/items/get?id=<TARGET>` XHR sometimes fires *after*
    the default networkidle window on production (Render US).  The browser-pool
    detail fetch must explicitly ``wait_for_response`` on that target response
    so the photo URL union post-pass always has target-scoped blobs to union
    from, otherwise we fall back to the og:image (one image) for many items.
    """
    captured = {"wait_for_response_predicates": []}

    async def fake_run_browser_page_task(site, task_coro_factory, **kwargs):
        class FakeResponse:
            status = 200
            url = "https://jp.mercari.com/item/m987654321"

        class FakePage:
            url = "https://jp.mercari.com/item/m987654321"

            def on(self, event, handler):
                pass

            async def goto(self, url, wait_until, timeout):
                return FakeResponse()

            async def wait_for_load_state(self, state, timeout):
                captured.setdefault("networkidle_timeouts", []).append(timeout)

            async def wait_for_selector(self, selector, timeout):
                pass

            async def wait_for_response(self, predicate, timeout):
                captured["wait_for_response_predicates"].append(predicate)
                captured["wait_for_response_timeout"] = timeout
                # Simulate /items/get?id=m987654321 firing before other
                # mercari responses to verify the predicate is target-scoped.
                class FakeRespMatch:
                    url = (
                        "https://api.mercari.jp/items/get?id=m987654321"
                        "&include_item_attributes=true"
                    )

                class FakeRespNonMatch:
                    url = (
                        "https://api.mercari.jp/v2/relateditems/list-similar-items"
                        "?id=m987654321"
                    )

                class FakeRespOtherItem:
                    url = (
                        "https://api.mercari.jp/items/get?id=m111111111"
                    )

                captured["predicate_match_target"] = predicate(FakeRespMatch())
                captured["predicate_match_related"] = predicate(FakeRespNonMatch())
                captured["predicate_match_other_item"] = predicate(FakeRespOtherItem())

            async def query_selector_all(self, selector):
                return []

            async def query_selector(self, selector):
                return None

            async def wait_for_timeout(self, ms):
                pass

            async def content(self):
                return "<html><body></body></html>"

        await task_coro_factory(FakePage(), object())

    monkeypatch.setattr(
        "services.mercari_browser_fetch.run_browser_page_task",
        fake_run_browser_page_task,
    )

    page, payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(
        "https://jp.mercari.com/item/m987654321"
    )

    # Verify the predicate correctly matches /items/get for the target item
    # id, and rejects related-items and other-item responses.
    assert captured["predicate_match_target"] is True
    assert captured["predicate_match_related"] is False
    assert captured["predicate_match_other_item"] is False
    assert captured["wait_for_response_timeout"] >= 10000, (
        "Must wait at least 10s for target /items/get; production needs >5s."
    )
    # Networkidle was also extended past the previous 5s value to catch late
    # XHRs.
    assert any(
        t >= 10000 for t in captured.get("networkidle_timeouts", [])
    ), "Primary networkidle wait must be >=10000ms on production."


def test_fetch_mercari_page_and_payloads_skips_wait_for_non_item_urls(monkeypatch):
    """Pricelist / shop / catalog URLs don't carry an ``m<digits>`` item id,
    so the ``wait_for_response`` should not be issued (it would pointlessly
    block for 15s)."""
    captured: dict = {}

    async def fake_run_browser_page_task(site, task_coro_factory, **kwargs):
        class FakeResponse:
            status = 200
            url = "https://jp.mercari.com/shop/abc"

        class FakePage:
            url = "https://jp.mercari.com/shop/abc"

            def on(self, event, handler):
                pass

            async def goto(self, url, wait_until, timeout):
                return FakeResponse()

            async def wait_for_load_state(self, state, timeout):
                pass

            async def wait_for_selector(self, selector, timeout):
                pass

            async def wait_for_response(self, predicate, timeout):
                captured["wait_for_response_called"] = True

            async def query_selector_all(self, selector):
                return []

            async def query_selector(self, selector):
                return None

            async def wait_for_timeout(self, ms):
                pass

            async def content(self):
                return "<html></html>"

        await task_coro_factory(FakePage(), object())

    monkeypatch.setattr(
        "services.mercari_browser_fetch.run_browser_page_task",
        fake_run_browser_page_task,
    )
    fetch_mercari_page_and_payloads_via_browser_pool_sync(
        "https://jp.mercari.com/shop/abc"
    )
    assert "wait_for_response_called" not in captured
