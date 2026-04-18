from services.mercari_browser_fetch import fetch_mercari_page_via_browser_pool_sync


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
                captured["wait_for_load_state"] = {
                    "state": state,
                    "timeout": timeout,
                }

            async def wait_for_selector(self, selector, timeout):
                captured["wait_for_selector"] = {
                    "selector": selector,
                    "timeout": timeout,
                }

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
