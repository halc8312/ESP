"""
Unit tests for Stage 1: Rakuma Playwright (Scrapling StealthyFetcher) migration.
Tests verify that rakuma_db.py and rakuma_patrol.py use StealthyFetcher
instead of Selenium.
"""
import sys
import types
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# Mock scrapling module to avoid transitive dependency issues in test env
# ---------------------------------------------------------------------------

_mock_scrapling = types.ModuleType("scrapling")
_mock_stealthy_fetcher = MagicMock()
_mock_scrapling.StealthyFetcher = _mock_stealthy_fetcher
_mock_scrapling.Fetcher = MagicMock()


@pytest.fixture(autouse=True)
def _patch_scrapling():
    """Ensure scrapling.StealthyFetcher.fetch is patchable in every test."""
    # We only need to ensure the import path works for patching
    # Save and restore original module if it exists
    original = sys.modules.get("scrapling")
    # Install a mock scrapling module for test patching
    mock_mod = types.ModuleType("scrapling")
    mock_mod.StealthyFetcher = MagicMock()
    mock_mod.Fetcher = MagicMock()
    sys.modules["scrapling"] = mock_mod

    # Also clear any cached imports of rakuma_db/rakuma_patrol so they re-import
    for key in list(sys.modules.keys()):
        if "rakuma_db" in key or "rakuma_patrol" in key:
            del sys.modules[key]

    yield mock_mod

    # Restore original
    if original is not None:
        sys.modules["scrapling"] = original
    else:
        sys.modules.pop("scrapling", None)

    # Clear cached imports again
    for key in list(sys.modules.keys()):
        if "rakuma_db" in key or "rakuma_patrol" in key:
            del sys.modules[key]


# ---------------------------------------------------------------------------
# rakuma_db.scrape_item_detail
# ---------------------------------------------------------------------------

def test_scrape_item_detail_structure(_patch_scrapling):
    """scrape_item_detail が正しい構造を返すことをモックで確認"""
    mock_el = MagicMock()
    mock_el.text = "テスト商品タイトル"

    mock_price_el = MagicMock()
    mock_price_el.text = "¥1,500"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト商品タイトル ¥1,500"

    def css_first_side_effect(selector):
        if "item__name" in selector or selector == "h1":
            return mock_el
        if "price" in selector:
            return mock_price_el
        return None

    mock_page.css_first.side_effect = css_first_side_effect
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test")

    assert "title" in result
    assert "price" in result
    assert "status" in result
    assert "image_urls" in result
    assert "description" in result
    assert "variants" in result
    assert isinstance(result["image_urls"], list)
    assert isinstance(result["variants"], list)
    assert result["url"] == "https://item.fril.jp/test"


def test_scrape_item_detail_title_extraction(_patch_scrapling):
    """scrape_item_detail がタイトルを正しく抽出することを確認"""
    mock_el = MagicMock()
    mock_el.text = "  テスト商品名  "

    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト商品名"

    def css_first_side_effect(selector):
        if "item__name" in selector or selector == "h1":
            return mock_el
        return None

    mock_page.css_first.side_effect = css_first_side_effect
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test")

    assert result["title"] == "テスト商品名"


def test_scrape_item_detail_price_extraction(_patch_scrapling):
    """scrape_item_detail が価格を正しく抽出することを確認"""
    mock_price_el = MagicMock()
    mock_price_el.text = "¥2,980"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥2,980"

    def css_first_side_effect(selector):
        if "price" in selector:
            return mock_price_el
        return None

    mock_page.css_first.side_effect = css_first_side_effect
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test")

    assert result["price"] == 2980


def test_scrape_item_detail_sold_status(_patch_scrapling):
    """scrape_item_detail が売り切れステータスを正しく検出することを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "この商品はSOLD OUTです"

    def css_first_side_effect(selector):
        return None

    mock_page.css_first.side_effect = css_first_side_effect
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/sold")

    assert result["status"] == "sold"


def test_scrape_item_detail_image_extraction(_patch_scrapling):
    """scrape_item_detail が画像URLを正しく抽出することを確認"""
    mock_img1 = MagicMock()
    mock_img1.attrib = {"src": "https://img.fril.jp/photo1.jpg"}
    mock_img2 = MagicMock()
    mock_img2.attrib = {"src": "", "data-lazy": "https://img.fril.jp/photo2.jpg"}

    mock_page = MagicMock()
    mock_page.get_text.return_value = "商品"

    def css_first_side_effect(selector):
        return None

    mock_page.css_first.side_effect = css_first_side_effect
    mock_page.css.return_value = [mock_img1, mock_img2]

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test")

    assert len(result["image_urls"]) == 2
    assert "https://img.fril.jp/photo1.jpg" in result["image_urls"]
    assert "https://img.fril.jp/photo2.jpg" in result["image_urls"]


def test_scrape_item_detail_error_handling(_patch_scrapling):
    """scrape_item_detail がエラー時に正しいレスポンスを返すことを確認"""
    _patch_scrapling.StealthyFetcher.fetch.side_effect = Exception("Connection error")

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/error")

    assert result["status"] == "error"
    assert result["title"] == ""
    assert result["price"] is None


def test_scrape_item_detail_driver_arg_ignored(_patch_scrapling):
    """scrape_item_detail が driver 引数を無視することを確認（後方互換性）"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "test"
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test", driver="fake_driver")

    # StealthyFetcher.fetch が呼ばれたことを確認（driver は使用されない）
    _patch_scrapling.StealthyFetcher.fetch.assert_called_once()


# ---------------------------------------------------------------------------
# rakuma_db.scrape_single_item
# ---------------------------------------------------------------------------

def test_scrape_single_item_returns_list(_patch_scrapling):
    """scrape_single_item がリストを返すことを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト ¥1,000"
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_single_item
    result = scrape_single_item("https://item.fril.jp/test")

    assert isinstance(result, list)
    assert len(result) == 1


def test_scrape_single_item_no_selenium(_patch_scrapling):
    """scrape_single_item が Selenium (create_driver) を使用しないことを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト"
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from rakuma_db import scrape_single_item

    # mercari_db.create_driver が呼ばれないことを確認
    with patch("mercari_db.create_driver") as mock_create_driver:
        result = scrape_single_item("https://item.fril.jp/test")
        mock_create_driver.assert_not_called()


# ---------------------------------------------------------------------------
# RakumaPatrol
# ---------------------------------------------------------------------------

def test_rakuma_patrol_uses_playwright(_patch_scrapling):
    """RakumaPatrol が Selenium ではなく Playwright(StealthyFetcher) を使用することを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥1,000 テスト商品"
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert result.success


def test_rakuma_patrol_price_extraction(_patch_scrapling):
    """RakumaPatrol が価格を正しく抽出することを確認"""
    mock_el = MagicMock()
    mock_el.text = "1,500"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥1,500 テスト商品"
    mock_page.css.return_value = [mock_el]

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert result.price == 1500


def test_rakuma_patrol_sold_status(_patch_scrapling):
    """RakumaPatrol が売り切れステータスを正しく検出することを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "SOLD OUT 売り切れ"
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert result.status == "sold"


def test_rakuma_patrol_error_handling(_patch_scrapling):
    """RakumaPatrol がエラーを正しく処理することを確認"""
    _patch_scrapling.StealthyFetcher.fetch.side_effect = Exception("Network error")

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert not result.success
    assert "Network error" in result.error


def test_rakuma_patrol_driver_arg_ignored(_patch_scrapling):
    """RakumaPatrol が driver 引数を無視することを確認（後方互換性）"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト"
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test", driver="fake_driver")

    # StealthyFetcher.fetch が呼ばれたことを確認
    _patch_scrapling.StealthyFetcher.fetch.assert_called_once()
    assert result.success


def test_fetch_rakuma_convenience_function(_patch_scrapling):
    """fetch_rakuma ヘルパー関数が正しく動作することを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト ¥500"
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.rakuma_patrol import fetch_rakuma
    result = fetch_rakuma("https://item.fril.jp/test")

    assert result.success


# ---------------------------------------------------------------------------
# BROWSER_SITES classification
# ---------------------------------------------------------------------------

def test_rakuma_removed_from_browser_sites():
    """Stage 1 完了: rakuma が BROWSER_SITES から削除されていることを確認"""
    from services.scrape_queue import BROWSER_SITES
    assert "rakuma" not in BROWSER_SITES
    assert "mercari" in BROWSER_SITES


# ---------------------------------------------------------------------------
# No Selenium imports in rakuma modules
# ---------------------------------------------------------------------------

def test_no_selenium_import_in_rakuma_db(_patch_scrapling):
    """rakuma_db.py に Selenium の import が含まれていないことを確認"""
    import ast
    import rakuma_db
    import inspect
    source = inspect.getsource(rakuma_db)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            assert "selenium" not in module, f"Found selenium import: {module}"
            assert "selenium" not in names, f"Found selenium import: {names}"
            assert "create_driver" not in names, f"Found create_driver import: {names}"


def test_no_selenium_import_in_rakuma_patrol(_patch_scrapling):
    """rakuma_patrol.py に Selenium の import が含まれていないことを確認"""
    import ast
    import services.patrol.rakuma_patrol as rp
    import inspect
    source = inspect.getsource(rp)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            assert "selenium" not in module, f"Found selenium import: {module}"
            assert "selenium" not in names, f"Found selenium import: {names}"
            assert "create_driver" not in names, f"Found create_driver import: {names}"
