from unittest.mock import MagicMock, patch


def _make_mock_page(
    *,
    title_text="テスト商品",
    page_title="テスト商品",
    body_text="¥1,000 テスト商品 購入手続きへ",
    meta_price=None,
    price_text="¥1,000",
    button_text="購入手続きへ",
    button_attrib=None,
):
    mock_page = MagicMock()

    meta_node = MagicMock()
    meta_node.text = ""
    meta_node.attrib = {"content": str(meta_price)} if meta_price is not None else {}

    title_node = MagicMock()
    title_node.text = title_text

    page_title_node = MagicMock()
    page_title_node.text = page_title

    body_node = MagicMock()
    body_node.text = body_text

    price_node = MagicMock()
    price_node.text = price_text

    button_node = MagicMock()
    button_node.text = button_text
    button_node.attrib = button_attrib or {"aria-disabled": "false"}

    def css_side_effect(selector):
        if selector == "meta[name='product:price:amount']":
            return [meta_node] if meta_price is not None else []
        if selector == "h1":
            return [title_node] if title_text else []
        if selector == "title":
            return [page_title_node] if page_title else []
        if selector == "body *":
            return [body_node] if body_text else []
        if selector == "[data-testid='price']":
            return [price_node] if price_text is not None else []
        if selector == "button":
            return [button_node] if button_text else []
        return []

    mock_page.css.side_effect = css_side_effect
    return mock_page


def test_fetch_active_product_prefers_meta_price():
    mock_page = _make_mock_page(meta_price=1000, price_text="")

    with patch("services.patrol.mercari_patrol.fetch_dynamic", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol

        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.success
    assert result.price == 1000
    assert result.status == "active"
    assert result.price_source == "meta"
    assert result.confidence == "high"


def test_fetch_sold_product():
    mock_page = _make_mock_page(
        body_text="売り切れ ¥1,000",
        button_text="購入手続きへ",
        button_attrib={"aria-disabled": "true"},
    )

    with patch("services.patrol.mercari_patrol.fetch_dynamic", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol

        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.success
    assert result.status == "sold"


def test_fetch_deleted_product_returns_successful_deleted_status():
    mock_page = _make_mock_page(
        title_text="",
        page_title="メルカリ - 日本最大のフリマサービス",
        body_text="該当する商品は削除されています 開始価格：¥300",
        price_text=None,
        button_text="",
    )

    with patch("services.patrol.mercari_patrol.fetch_dynamic", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol

        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.success
    assert result.status == "deleted"
    assert result.price is None


def test_fetch_active_product_without_price_returns_failure():
    mock_page = _make_mock_page(
        meta_price=None,
        price_text="",
        body_text="テスト商品 購入手続きへ",
    )

    with patch("services.patrol.mercari_patrol.fetch_dynamic", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol

        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert not result.success
    assert result.status == "active"


def test_fetch_error_handling():
    with patch("services.patrol.mercari_patrol.fetch_dynamic", side_effect=Exception("Connection error")):
        from services.patrol.mercari_patrol import MercariPatrol

        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert not result.success
    assert result.error is not None


def test_fetch_can_use_browser_pool(monkeypatch):
    mock_page = _make_mock_page(meta_price=1000, price_text="")
    monkeypatch.setenv("MERCARI_PATROL_USE_BROWSER_POOL", "true")

    with patch(
        "services.patrol.mercari_patrol.fetch_mercari_page_via_browser_pool_sync",
        return_value=mock_page,
    ) as mock_pool_fetch, patch("services.patrol.mercari_patrol.fetch_dynamic") as mock_fetch_dynamic:
        from services.patrol.mercari_patrol import MercariPatrol

        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")

    mock_pool_fetch.assert_called_once_with("https://jp.mercari.com/item/xxx", network_idle=False)
    mock_fetch_dynamic.assert_not_called()
    assert result.success
    assert result.price == 1000


def test_fetch_mercari_shops_uses_shops_scraper():
    shops_item = {
        "url": "https://jp.mercari.com/shops/product/test",
        "title": "Shops Item",
        "price": 4090,
        "status": "on_sale",
        "description": "",
        "image_urls": [],
        "variants": [{"name": "Default Title", "stock": 1, "price": 4090}],
        "_scrape_meta": {"confidence": "high", "reasons": ["purchase-flow"], "price_source": "dom"},
    }

    with patch("services.patrol.mercari_patrol.scrape_shops_product", return_value=shops_item) as mock_scrape_shops, patch(
        "services.patrol.mercari_patrol.fetch_dynamic"
    ) as mock_fetch_dynamic:
        from services.patrol.mercari_patrol import MercariPatrol

        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/shops/product/test")

    mock_scrape_shops.assert_called_once_with("https://jp.mercari.com/shops/product/test")
    mock_fetch_dynamic.assert_not_called()
    assert result.success
    assert result.price == 4090
    assert result.status == "active"


def test_monitor_service_no_driver():
    from services.monitor_service import _BROWSER_SITES

    assert "mercari" not in _BROWSER_SITES
