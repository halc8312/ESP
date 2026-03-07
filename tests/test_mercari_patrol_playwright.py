"""
Unit tests for MercariPatrol (services/patrol/mercari_patrol.py).
Verifies Stage 2: Selenium → Playwright (Scrapling StealthyFetcher) migration.
"""
import sys
import types
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Mock scrapling module to avoid transitive dependency issues in test env
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_scrapling():
    """Patch scrapling and scrapling.fetchers modules for every test."""
    original = sys.modules.get("scrapling")
    original_fetchers = sys.modules.get("scrapling.fetchers")

    # Install mock scrapling module
    mock_mod = types.ModuleType("scrapling")
    mock_mod.StealthyFetcher = MagicMock()
    mock_mod.Fetcher = MagicMock()

    # Install mock scrapling.fetchers module
    mock_fetchers_mod = types.ModuleType("scrapling.fetchers")
    mock_fetchers_mod.AsyncFetcher = MagicMock()

    sys.modules["scrapling"] = mock_mod
    sys.modules["scrapling.fetchers"] = mock_fetchers_mod

    # Clear any cached imports of mercari_patrol so it re-imports with mock
    for key in list(sys.modules.keys()):
        if "mercari_patrol" in key:
            del sys.modules[key]

    yield mock_mod

    # Restore originals
    if original is not None:
        sys.modules["scrapling"] = original
    else:
        sys.modules.pop("scrapling", None)

    if original_fetchers is not None:
        sys.modules["scrapling.fetchers"] = original_fetchers
    else:
        sys.modules.pop("scrapling.fetchers", None)

    # Clear cached imports again
    for key in list(sys.modules.keys()):
        if "mercari_patrol" in key:
            del sys.modules[key]


def _make_mock_page(
    body_text="¥1,000 テスト商品",
    price_text="¥1,000",
    is_sold=False,
    variants=None,
):
    """テスト用モックページオブジェクトを作成"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = body_text

    # price 要素
    price_el = MagicMock()
    price_el.text = price_text
    mock_page.css_first.return_value = price_el

    # 購入ボタン
    btn = MagicMock()
    btn.text = "購入手続きへ"
    btn.attrib = {} if not is_sold else {"disabled": ""}

    # バリエーションラベル
    var_labels = variants if variants is not None else []

    def css_side_effect(selector):
        if selector == "button":
            return [btn]
        if selector == "[data-testid='variation-label']":
            return var_labels
        return []

    mock_page.css.side_effect = css_side_effect

    return mock_page


# ---------------------------------------------------------------------------
# MercariPatrol.fetch — 基本動作
# ---------------------------------------------------------------------------

def test_fetch_active_product(_patch_scrapling):
    """販売中の商品を正しく取得できることを確認"""
    mock_page = _make_mock_page()
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.success
    assert result.price == 1000
    assert result.status == "active"


def test_fetch_sold_product(_patch_scrapling):
    """売り切れ商品を正しく判定できることを確認"""
    mock_page = _make_mock_page(body_text="売り切れ ¥1,000")
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.success
    assert result.status == "sold"


def test_fetch_error_handling(_patch_scrapling):
    """ネットワークエラー時に PatrolResult(error=...) を返すことを確認"""
    _patch_scrapling.StealthyFetcher.fetch.side_effect = Exception("Connection error")

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert not result.success
    assert result.error is not None
    assert "Connection error" in result.error


def test_fetch_uses_stealthy_fetcher(_patch_scrapling):
    """MercariPatrol が StealthyFetcher を使用することを確認"""
    mock_page = _make_mock_page()
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    patrol.fetch("https://jp.mercari.com/item/xxx")

    _patch_scrapling.StealthyFetcher.fetch.assert_called_once()
    call_kwargs = _patch_scrapling.StealthyFetcher.fetch.call_args
    assert call_kwargs.kwargs.get("headless") is True


def test_fetch_driver_arg_ignored(_patch_scrapling):
    """MercariPatrol が driver 引数を無視することを確認（後方互換性）"""
    mock_page = _make_mock_page()
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx", driver="fake_driver")

    _patch_scrapling.StealthyFetcher.fetch.assert_called_once()
    assert result.success


# ---------------------------------------------------------------------------
# _extract_price
# ---------------------------------------------------------------------------

def test_extract_price_from_css_selector(_patch_scrapling):
    """data-testid='price' セレクタから価格を取得できることを確認"""
    mock_page = _make_mock_page(price_text="¥2,980")
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.price == 2980


def test_extract_price_fallback_body(_patch_scrapling):
    """CSS セレクタが null の場合、body テキストから価格を取得することを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "この商品は¥3,500です"
    mock_page.css_first.return_value = None  # no price element
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.price == 3500


def test_extract_price_none_when_unavailable(_patch_scrapling):
    """価格情報がない場合、None を返すことを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト商品"
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.price is None


# ---------------------------------------------------------------------------
# _extract_status
# ---------------------------------------------------------------------------

def test_extract_status_sold_by_body_text(_patch_scrapling):
    """body テキストの「売り切れ」でステータスが sold になることを確認"""
    mock_page = _make_mock_page(body_text="売り切れ")
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.status == "sold"


def test_extract_status_sold_by_english(_patch_scrapling):
    """body テキストの「Sold」でステータスが sold になることを確認"""
    mock_page = _make_mock_page(body_text="Sold ¥1,000")
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.status == "sold"


def test_extract_status_sold_by_disabled_button(_patch_scrapling):
    """購入ボタンが disabled の場合、ステータスが sold になることを確認"""
    mock_page = _make_mock_page(body_text="テスト商品 ¥1,000", is_sold=True)
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.status == "sold"


def test_extract_status_unknown_empty_body(_patch_scrapling):
    """body テキストが空の場合、ステータスが unknown になることを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = ""
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.status == "unknown"


# ---------------------------------------------------------------------------
# _extract_variants
# ---------------------------------------------------------------------------

def test_extract_variants_active(_patch_scrapling):
    """バリエーション情報（在庫あり）を正しく取得できることを確認"""
    label = MagicMock()
    label.text = "  赤  "
    label.html = "<span>赤</span>"
    label.attrib = {"class": "variation-label"}

    mock_page = _make_mock_page(variants=[label])
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert len(result.variants) == 1
    assert result.variants[0]["name"] == "赤"
    assert result.variants[0]["stock"] == 1


def test_extract_variants_sold(_patch_scrapling):
    """バリエーション情報（売り切れ）を正しく取得できることを確認"""
    label = MagicMock()
    label.text = "青"
    label.html = "<span>青 売り切れ</span>"
    label.attrib = {"class": "variation-label"}

    mock_page = _make_mock_page(variants=[label])
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.variants[0]["stock"] == 0


def test_extract_variants_disabled_class(_patch_scrapling):
    """class に 'disabled' が含まれる場合、在庫 0 になることを確認"""
    label = MagicMock()
    label.text = "緑"
    label.html = "<span>緑</span>"
    label.attrib = {"class": "variation-label disabled"}

    mock_page = _make_mock_page(variants=[label])
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.variants[0]["stock"] == 0


def test_extract_variants_empty(_patch_scrapling):
    """バリエーションが存在しない場合、空リストを返すことを確認"""
    mock_page = _make_mock_page(variants=[])
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import MercariPatrol
    patrol = MercariPatrol()
    result = patrol.fetch("https://jp.mercari.com/item/xxx")

    assert result.variants == []


# ---------------------------------------------------------------------------
# fetch_mercari convenience function
# ---------------------------------------------------------------------------

def test_fetch_mercari_convenience_function(_patch_scrapling):
    """fetch_mercari ヘルパー関数が正しく動作することを確認"""
    mock_page = _make_mock_page()
    _patch_scrapling.StealthyFetcher.fetch.return_value = mock_page

    from services.patrol.mercari_patrol import fetch_mercari
    result = fetch_mercari("https://jp.mercari.com/item/xxx")

    assert result.success


# ---------------------------------------------------------------------------
# monitor_service: _BROWSER_SITES に mercari が含まれないことを確認
# ---------------------------------------------------------------------------

def test_monitor_service_no_driver(_patch_scrapling):
    """Stage 2 完了後、_BROWSER_SITES に mercari が含まれないことを確認"""
    from services.monitor_service import _BROWSER_SITES
    assert "mercari" not in _BROWSER_SITES


# ---------------------------------------------------------------------------
# Selenium import が存在しないことを確認
# ---------------------------------------------------------------------------

def test_no_selenium_import_in_mercari_patrol(_patch_scrapling):
    """mercari_patrol.py に Selenium の import が含まれていないことを確認"""
    import ast
    import inspect
    import services.patrol.mercari_patrol as mp
    source = inspect.getsource(mp)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            assert "selenium" not in module, f"Found selenium import: {module}"
            assert "selenium" not in names, f"Found selenium import: {names}"
            assert "create_driver" not in names, f"Found create_driver import: {names}"
