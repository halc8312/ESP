import pytest
from unittest.mock import patch, MagicMock

def _make_mock_page(
    body_text="¥1,000 テスト商品",
    price_text="¥1,000",
    is_sold=False,
):
    """テスト用モックページオブジェクトを作成"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = body_text
    
    # price 要素
    price_el = MagicMock()
    price_el.text = price_text
    
    def price_css_side_effect(selector):
        if selector == "[data-testid='price']":
            return [price_el]
        return []
    
    # buttons
    btn = MagicMock()
    btn.text = "購入手続きへ"
    btn.attrib = {"disabled": None} if not is_sold else {"disabled": ""}
    
    def btn_css_side_effect(selector):
        if selector == "button":
            return [btn]
        return price_css_side_effect(selector)
        
    mock_page.css.side_effect = btn_css_side_effect
    
    return mock_page

def test_fetch_active_product():
    """販売中の商品を正しく取得できることを確認"""
    mock_page = _make_mock_page(price_text="¥1,000")
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol
        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")
    
    assert result.success
    assert result.price == 1000
    assert result.status == "active"

def test_fetch_sold_product():
    """売り切れ商品を正しく判定できることを確認"""
    mock_page = _make_mock_page(body_text="売り切れ ¥1,000", is_sold=True)
    
    with patch("scrapling.StealthyFetcher.fetch", return_value=mock_page):
        from services.patrol.mercari_patrol import MercariPatrol
        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")
    
    assert result.success
    assert result.status == "sold"

def test_fetch_error_handling():
    """ネットワークエラー時に PatrolResult(error=...) を返すことを確認"""
    with patch("scrapling.StealthyFetcher.fetch", side_effect=Exception("Connection error")):
        from services.patrol.mercari_patrol import MercariPatrol
        patrol = MercariPatrol()
        result = patrol.fetch("https://jp.mercari.com/item/xxx")
    
    assert not result.success
    assert result.error is not None

def test_monitor_service_no_driver():
    """monitor_service が driver を作成しなくなったことを確認"""
    from services.monitor_service import _BROWSER_SITES
    assert "mercari" not in _BROWSER_SITES
