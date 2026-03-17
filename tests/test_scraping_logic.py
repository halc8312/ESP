import pytest
from unittest.mock import MagicMock, patch
from mercari_db import (
    _extract_price_from_text,
    _extract_plain_number_from_text,
    _extract_mercari_shops_title_from_body,
    _infer_mercari_shops_status,
    _normalize_mercari_shops_title,
    scrape_search_result,
    scrape_item_detail,
    scrape_shops_product,
)

# --- Test Cases for Playwright / Scrapling Implementation ---

def test_scrape_search_result_count_guarantee():
    """
    Test that the search loop continues until max_items is reached.
    We test this by mocking the internal async wrapper.
    """
    max_items = 5
    
    # mock _scrape_search_async which returns a list of URLs
    mock_urls = [
        "http://m/item/1",
        "http://m/item/2",
        "http://m/item/3",
        "http://m/item/4",
        "http://m/item/5",
        "http://m/item/6",
    ]
    
    with patch('mercari_db._scrape_search_async') as mock_search:
        # We need to mock the async function to return a coroutine
        async def mock_search_async(*args, **kwargs):
            return mock_urls
        mock_search.side_effect = mock_search_async

        # Mock individual item details
        with patch('mercari_db.scrape_item_detail') as mock_detail:
            mock_detail.side_effect = [
                {"title": "Item 1", "status": "on_sale", "url": "http://m/item/1"},
                {"title": "Item 2", "status": "on_sale", "url": "http://m/item/2"},
                {"title": "", "status": "error", "url": "http://m/item/3"},       # Failed item
                {"title": "Item 4", "status": "on_sale", "url": "http://m/item/4"},
                {"title": "Item 5", "status": "on_sale", "url": "http://m/item/5"},
                {"title": "Item 6", "status": "on_sale", "url": "http://m/item/6"},
            ]
            
            results = scrape_search_result("http://search", max_items=max_items, max_scroll=2)
            
            assert len(results) == max_items
            assert results[0]["title"] == "Item 1"
            assert results[2]["title"] == "Item 4" # Item 3 skipped


def test_scrape_variants_pattern_detection():
    """
    Test that scrape_item_detail detects variants using Scrapling Response mock.
    """
    url = "http://m/item/variant"
    
    with patch('mercari_db.fetch_dynamic') as mock_fetch:
        mock_page = MagicMock()
        mock_fetch.return_value = mock_page
        
        # Mock title
        mock_title = MagicMock()
        mock_title.text = "Variant T-Shirt"
        
        # Mock variants button
        btn1 = MagicMock(); btn1.text = "Red"
        btn2 = MagicMock(); btn2.text = "Blue"
        
        # Configure .css() behavior
        def mock_css(selector):
            if selector == "h1":
                return [mock_title]
            elif "button" in selector or "div" in selector:
                # Return variants only for specific selector path to avoid infinite matching
                if "mer-item-thumbnail" in selector or "radiogroup" in selector:
                   return [btn1, btn2]
            return []
            
        mock_page.css.side_effect = mock_css
        mock_page.get_text.return_value = "Some description body text. 購入手続きへ"
        
        # Call function
        data = scrape_item_detail(url)
         
        # Verification
        assert data["title"] == "Variant T-Shirt"
        assert len(data["variants"]) == 2
        assert data["variants"][0]["option1_value"] == "Red"
        assert data["variants"][1]["option1_value"] == "Blue"


def test_scrape_variants_shops_pattern():
    """
    Test Mercari Shops pattern by mocking the internal async wrapper function.
    """
    url = "http://m/shops/product/variant" 
    
    with patch('mercari_db._scrape_shops_product_async') as mock_shops_async:
        async def mock_shops_impl(*args, **kwargs):
             return {
                 "url": url,
                 "title": "Shops Item",
                 "price": 1000,
                 "status": "on_sale",
                 "description": "desc",
                 "image_urls": [],
                 "variants": [
                      {"option1_name": "サイズ", "option1_value": "Size S"},
                      {"option1_name": "サイズ", "option1_value": "Size M"}
                 ]
             }
        mock_shops_async.side_effect = mock_shops_impl
        
        # Call the sync wrapper directly
        data = scrape_shops_product(url)

        assert data["title"] == "Shops Item"
        assert len(data["variants"]) == 2
        assert data["variants"][0]["option1_value"] == "Size S"


def test_mercari_shops_price_fallback_supports_yen_symbol():
    assert _extract_price_from_text("¥\n2,980\n送料込み") == 2980


def test_extract_price_from_text_ignores_comma_only_capture():
    assert _extract_price_from_text(",,,") is None


def test_extract_plain_number_from_text_supports_comma_separated_digits():
    assert _extract_plain_number_from_text("8,980") == 8980


def test_extract_plain_number_from_text_ignores_comma_only_text():
    assert _extract_plain_number_from_text(",,,") is None


def test_scrape_item_detail_tolerates_invalid_price_text():
    url = "http://m/item/invalid-price"

    with patch('mercari_db.fetch_dynamic') as mock_fetch:
        mock_page = MagicMock()
        mock_fetch.return_value = mock_page

        mock_title = MagicMock()
        mock_title.text = "Valid Item"

        mock_price = MagicMock()
        mock_price.text = ",,,"

        mock_body_node = MagicMock()
        mock_body_node.text = "購入手続きへ"

        def mock_css(selector):
            if selector == "h1":
                return [mock_title]
            if selector == "[data-testid='price']":
                return [mock_price]
            if selector == "body *":
                return [mock_body_node]
            if selector == "button":
                return []
            return []

        mock_page.css.side_effect = mock_css

        data = scrape_item_detail(url)

        assert data["title"] == "Valid Item"
        assert data["price"] is None
        assert data["status"] == "on_sale"


def test_scrape_item_detail_ignores_deleted_page_shell_price():
    url = "http://m/item/deleted"

    with patch('mercari_db.fetch_dynamic') as mock_fetch:
        mock_page = MagicMock()
        mock_fetch.return_value = mock_page

        missing_node = MagicMock()
        missing_node.text = "該当する商品は削除されています"
        shell_price_node = MagicMock()
        shell_price_node.text = "開始価格：¥300"
        title_node = MagicMock()
        title_node.text = "メルカリ - 日本最大のフリマサービス"

        def mock_css(selector):
            if selector == "body *":
                return [missing_node, shell_price_node]
            if selector == "title":
                return [title_node]
            return []

        mock_page.css.side_effect = mock_css

        data = scrape_item_detail(url)

        assert data["price"] is None
        assert data["status"] == "deleted"


def test_scrape_item_detail_prefers_meta_price_when_dom_text_is_empty():
    url = "http://m/item/meta-price"

    with patch('mercari_db.fetch_dynamic') as mock_fetch:
        mock_page = MagicMock()
        mock_fetch.return_value = mock_page

        meta_node = MagicMock()
        meta_node.text = ""
        meta_node.attrib = {"content": "2500"}

        title_node = MagicMock()
        title_node.text = "Valid Item"

        purchase_button = MagicMock()
        purchase_button.text = "購入手続きへ"
        purchase_button.attrib = {"aria-disabled": "false"}

        def mock_css(selector):
            if selector == "meta[name='product:price:amount']":
                return [meta_node]
            if selector == "h1":
                return [title_node]
            if selector == "button":
                return [purchase_button]
            if selector in {"body *", "[data-testid='price']", "script[type='application/ld+json']", "title"}:
                return []
            return []

        mock_page.css.side_effect = mock_css

        data = scrape_item_detail(url)

        assert data["price"] == 2500
        assert data["status"] == "on_sale"


def test_mercari_shops_title_fallback_uses_document_title():
    assert _normalize_mercari_shops_title("テスト商品 - メルカリ") == "テスト商品"


def test_mercari_shops_title_fallback_can_extract_from_body():
    body_text = """
    コンテンツにスキップ
    メルカリShops
    1 / 9
    テスト商品タイトル
    ¥2,980
    送料込み
    """
    assert _extract_mercari_shops_title_from_body(body_text) == "テスト商品タイトル"


def test_mercari_shops_status_prefers_purchase_flow_over_partial_sold_variants():
    body_text = """
    テスト商品
    売り切れ
    残り1点
    購入手続きへ
    """
    assert _infer_mercari_shops_status(body_text) == "on_sale"


def test_mercari_shops_status_detects_full_sold_out():
    body_text = """
    この商品は売り切れです
    在庫なし
    """
    assert _infer_mercari_shops_status(body_text) == "sold"

