import pytest
from unittest.mock import MagicMock, patch
from mercari_db import scrape_search_result, scrape_item_detail

# --- Fixtures for Mocking Selenium ---

@pytest.fixture
def mock_driver():
    with patch('mercari_db.create_driver') as mock_create:
        driver = MagicMock()
        mock_create.return_value = driver
        yield driver

# --- Test Cases ---

def test_scrape_search_result_count_guarantee(mock_driver):
    """
    Test that the search loop continues until max_items is reached.
    Scenario:
    - User wants 5 items.
    - First scroll finds 3 valid links.
    - Second scroll finds 3 more links (total 6 unique).
    - Scraping these links results in 1 failure and 5 successes.
    - Result should be exactly 5 items.
    """
    max_items = 5
    
    # Mock search page navigation
    mock_driver.title = "Search Results"
    
    # Mock finding links (simplified)
    # 1st call: 3 links
    # 2nd call: 3 new links
    link1 = MagicMock(); link1.get_attribute.return_value = "http://m/item/1"
    link2 = MagicMock(); link2.get_attribute.return_value = "http://m/item/2"
    link3 = MagicMock(); link3.get_attribute.return_value = "http://m/item/3"
    link4 = MagicMock(); link4.get_attribute.return_value = "http://m/item/4"
    link5 = MagicMock(); link5.get_attribute.return_value = "http://m/item/5"
    link6 = MagicMock(); link6.get_attribute.return_value = "http://m/item/6"
    
    mock_driver.find_elements.side_effect = [
        [link1, link2, link3], # First find_elements call
        [link4, link5, link6], # Second find_elements call (after scroll)
        [], [], [], [], [] # Subsequent calls return empty
    ]
    
    # Mock individual item details
    # We mock scrape_item_detail to avoid mocking internal item page DOM
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

def test_scrape_variants_pattern_detection(mock_driver):
    """
    Test that scrape_item_detail detects variants using the new selectors.
    We'll assume the driver returns elements matching our patterns.
    """
    url = "http://m/item/variant"
    mock_driver.get.return_value = None
    
    # Mock finding elements for title, price etc.
    mock_driver.find_elements.return_value = [] # Default for un-mocked find_elements
    
    # Mock selectors loop in scrape_item_detail
    # We need to craft the mock such that when searching for variant buttons, it returns something.
    
    def side_effect_find_elements(by, value):
        if "カラー" in value and "button" in value:
            btn1 = MagicMock(); btn1.text = "Red"
            btn2 = MagicMock(); btn2.text = "Blue"
            return [btn1, btn2]
        return []

    mock_driver.find_elements.side_effect = side_effect_find_elements

    # Mock WebDriverWait.until to return a mock element with text
    with patch('selenium.webdriver.support.ui.WebDriverWait.until') as mock_until:
        # For title
        title_mock = MagicMock()
        title_mock.text = "Variant T-Shirt"
        
        # When until() is called, it returns our title_mock
        mock_until.return_value = title_mock
        
        # Also need to handle body text if accessed via wait or find_element
        mock_driver.find_element.return_value.text = "Some description body text"
        
        # Call function
        data = scrape_item_detail(mock_driver, url)
         
        # Verification
        assert data["title"] == "Variant T-Shirt"
        assert len(data["variants"]) == 2
        assert data["variants"][0]["option1_value"] == "Red"

def test_scrape_variants_shops_pattern(mock_driver):
    """
    Test Mercari Shops pattern (mer-item-thumbnail)
    """
    url = "http://m/shops/product/variant" 
    
    # Logic redirects to scrape_shops_product if url has /shops/product/
    
    def side_effect_find_elements(by, value):
        if "mer-item-thumbnail" in value:
            btn1 = MagicMock(); btn1.text = "Size S"
            btn2 = MagicMock(); btn2.text = "Size M"
            return [btn1, btn2]
        # For title in Shops
        if "product-name" in value:
             t = MagicMock(); t.text = "Shops Item"
             return [t]
        return []

    mock_driver.find_elements.side_effect = side_effect_find_elements
    
    # Call directly
    from mercari_db import scrape_shops_product
    data = scrape_shops_product(mock_driver, url)

    assert data["title"] == "Shops Item"
    assert len(data["variants"]) == 2
    assert data["variants"][0]["option1_value"] == "Size S"
