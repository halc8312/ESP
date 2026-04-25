"""
Unit tests for Rakuma scraping module (rakuma_db.py) and patrol (rakuma_patrol.py).
Tests verify that:
  - rakuma_db.py uses Fetcher/AsyncFetcher (HTTP) for item detail pages (SSR)
  - rakuma_patrol.py uses HTTP fetches for patrol checks
"""
import json
import sys
import types
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


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
    mock_async_fetcher = MagicMock()
    mock_async_fetcher.get = AsyncMock()
    mock_fetchers_mod.AsyncFetcher = mock_async_fetcher

    sys.modules["scrapling"] = mock_mod
    sys.modules["scrapling.fetchers"] = mock_fetchers_mod

    # Clear any cached imports of rakuma_db/rakuma_patrol so they re-import
    for key in list(sys.modules.keys()):
        if "rakuma_db" in key or "rakuma_patrol" in key:
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

    _patch_scrapling.Fetcher.get.return_value = mock_page

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

    _patch_scrapling.Fetcher.get.return_value = mock_page

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

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test")

    assert result["price"] == 2980


def test_scrape_item_detail_prefers_meta_price_over_body_recommendations(_patch_scrapling):
    """Rakuma は body-wide regex ではなく product meta price を優先する。"""
    title_el = MagicMock()
    title_el.text = "NIKE ナイキ エアフォース1 カーキ 26.5cm"

    meta_price_el = MagicMock()
    meta_price_el.text = ""
    meta_price_el.attrib = {"content": "7000"}

    jsonld_el = MagicMock()
    jsonld_el.text = json.dumps(
        {
            "@context": "http://schema.org/",
            "@type": "Product",
            "name": "NIKE ナイキ エアフォース1 カーキ 26.5cm",
            "offers": {"@type": "Offer", "price": 7000, "availability": "http://schema.org/InStock"},
        }
    )

    mock_page = MagicMock()
    mock_page.get_text.return_value = """
        おすすめ商品
        ¥48,800
        Fragment × Union × Nike Air Jordan 1
        ¥67,800
        NIKE ナイキ エアフォース1 カーキ 26.5cm
    """

    def css_side_effect(selector):
        if selector == "meta[property='product:price:amount']":
            return [meta_price_el]
        if selector == "script[type='application/ld+json']":
            return [jsonld_el]
        if selector in {"h1.item__name", "h1"}:
            return [title_el]
        return []

    mock_page.css.side_effect = css_side_effect
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail

    result = scrape_item_detail("https://item.fril.jp/test")

    assert result["price"] == 7000
    assert result["status"] == "on_sale"


def test_scrape_item_detail_sold_status(_patch_scrapling):
    """SOLD selector は stale な InStock JSON-LD より優先される。"""
    sold_el = MagicMock()
    sold_el.text = "SOLDOUT"

    jsonld_el = MagicMock()
    jsonld_el.text = json.dumps(
        {
            "@context": "http://schema.org/",
            "@type": "Product",
            "name": "Rakuma Sold Item",
            "offers": {"@type": "Offer", "price": 5840, "availability": "http://schema.org/InStock"},
        }
    )

    mock_page = MagicMock()
    mock_page.get_text.return_value = "この商品は売り切れです"

    def css_side_effect(selector):
        if selector == "span.soldout":
            return [sold_el]
        if selector == "script[type='application/ld+json']":
            return [jsonld_el]
        return []

    mock_page.css.side_effect = css_side_effect
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/sold")

    assert result["status"] == "sold"


def test_scrape_item_detail_does_not_treat_description_sold_word_as_sold(_patch_scrapling):
    """説明文の「売り切れ」で sold に倒さず、購入導線を優先する。"""
    title_el = MagicMock()
    title_el.text = "Rakuma Active Item"

    buy_el = MagicMock()
    buy_el.text = "購入に進む"

    jsonld_el = MagicMock()
    jsonld_el.text = json.dumps(
        {
            "@context": "http://schema.org/",
            "@type": "Product",
            "name": "Rakuma Active Item",
            "offers": {"@type": "Offer", "price": 2681, "availability": "http://schema.org/InStock"},
        }
    )

    mock_page = MagicMock()
    mock_page.get_text.return_value = """
        Rakuma Active Item
        送料込 すぐに購入可
        購入に進む
        商品説明
        他サイトでも販売しているため売り切れの可能性があります
    """

    def css_side_effect(selector):
        if selector == "script[type='application/ld+json']":
            return [jsonld_el]
        if selector in {"h1.item__name", "h1"}:
            return [title_el]
        if selector in {"a.btn_buy", ".ga-item-buybutton", "a[href*='ref_action=btn_buy']"}:
            return [buy_el]
        return []

    mock_page.css.side_effect = css_side_effect
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/active")

    assert result["status"] == "on_sale"


def test_scrape_item_detail_image_extraction(_patch_scrapling):
    """scrape_item_detail が画像URLを正しく抽出することを確認"""
    mock_img1 = MagicMock()
    mock_img1.attrib = {"src": "https://img.fril.jp/img/111111111/l/222222221.jpg?1"}
    mock_img2 = MagicMock()
    mock_img2.attrib = {"src": "", "data-lazy": "https://img.fril.jp/img/111111111/l/222222222.jpg?2"}

    mock_page = MagicMock()
    mock_page.get_text.return_value = "商品"

    def css_first_side_effect(selector):
        return None

    def css_side_effect(selector):
        if selector in {
            ".sp-image",
            ".soldout-section .image",
            "img[src*='img.fril.jp']",
            ".item-box--image img",
            ".item-box--image-main img",
            "img[class*='item-image']",
            "img[src*='fril']",
        }:
            return [mock_img1, mock_img2]
        return []

    mock_page.css_first.side_effect = css_first_side_effect
    mock_page.css.side_effect = css_side_effect

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test")

    assert len(result["image_urls"]) == 2
    assert "https://img.fril.jp/img/111111111/l/222222221.jpg?1" in result["image_urls"]
    assert "https://img.fril.jp/img/111111111/l/222222222.jpg?2" in result["image_urls"]


def test_scrape_item_detail_accepts_scrapling_attribute_handler_images(_patch_scrapling):
    class AttributeLike:
        def __init__(self, values):
            self._values = values

        def get(self, key, default=None):
            return self._values.get(key, default)

        def items(self):
            return self._values.items()

    def image_node(values):
        node = MagicMock()
        node.attrib = AttributeLike(values)
        return node

    jsonld_el = MagicMock()
    jsonld_el.text = json.dumps(
        {
            "@type": "Product",
            "name": "複数画像の商品",
            "image": "https://img.fril.jp/img/827839210/m/2847527998.jpg?1777001980",
        }
    )

    product_imgs = [
        image_node({"src": "https://img.fril.jp/img/827839210/l/2847527998.jpg?1777001980"}),
        image_node({"data-lazy": "https://img.fril.jp/img/827839210/l/2847527999.jpg?1777001981"}),
        image_node({"srcset": "https://img.fril.jp/img/827839210/m/2847528000.jpg?1777001982 1x"}),
    ]
    related_img = image_node({"src": "https://img.fril.jp/img/999999999/l/related.jpg?1"})

    mock_page = MagicMock()
    mock_page.get_text.return_value = "複数画像の商品"
    mock_page.css_first.return_value = None

    def css_side_effect(selector):
        if selector == "script[type='application/ld+json']":
            return [jsonld_el]
        if selector == ".sp-image":
            return product_imgs
        if selector == "img[src*='img.fril.jp']":
            return product_imgs + [related_img]
        return []

    mock_page.css.side_effect = css_side_effect

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/0b1b0c2af2bc96e3fd97f1b3e1b683ce")

    assert result["image_urls"] == [
        "https://img.fril.jp/img/827839210/l/2847527998.jpg?1777001980",
        "https://img.fril.jp/img/827839210/l/2847527999.jpg?1777001981",
        "https://img.fril.jp/img/827839210/m/2847528000.jpg?1777001982",
    ]


def test_scrape_item_detail_error_handling(_patch_scrapling):
    """scrape_item_detail がエラー時に正しいレスポンスを返すことを確認"""
    _patch_scrapling.Fetcher.get.side_effect = Exception("Connection error")

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/error")

    assert result["status"] == "error"
    assert result["title"] == ""
    assert result["price"] is None


def test_scrape_item_detail_missing_item_returns_deleted(_patch_scrapling):
    mock_page = MagicMock()
    mock_page.get_text.return_value = "お探しの商品は見つかりません"
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/missing")

    assert result["status"] == "deleted"
    assert result["price"] is None


def test_scrape_item_detail_driver_arg_ignored(_patch_scrapling):
    """scrape_item_detail が driver 引数を無視することを確認（後方互換性）"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "test"
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_item_detail
    result = scrape_item_detail("https://item.fril.jp/test", driver="fake_driver")

    # Fetcher.get が呼ばれたことを確認（driver は使用されない）
    _patch_scrapling.Fetcher.get.assert_called_once()


# ---------------------------------------------------------------------------
# rakuma_db.scrape_single_item
# ---------------------------------------------------------------------------

def test_scrape_single_item_returns_list(_patch_scrapling):
    """scrape_single_item がリストを返すことを確認"""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト ¥1,000"
    mock_page.css_first.return_value = None
    mock_page.css.return_value = []

    _patch_scrapling.Fetcher.get.return_value = mock_page

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

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from rakuma_db import scrape_single_item
    import mercari_db

    # Stage 3 完了後は create_driver 自体が存在しない。互換テストのため create=True で監視する。
    with patch.object(mercari_db, "create_driver", create=True) as mock_create_driver:
        result = scrape_single_item("https://item.fril.jp/test")
        mock_create_driver.assert_not_called()


# ---------------------------------------------------------------------------
# RakumaPatrol
# ---------------------------------------------------------------------------

def test_rakuma_patrol_uses_http_fetcher(_patch_scrapling):
    """RakumaPatrol が Playwright ではなく HTTP Fetcher を使用することを確認"""
    mock_title_el = MagicMock()
    mock_title_el.text = "テスト商品"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥1,000 テスト商品"
    mock_page.css.side_effect = lambda selector: [mock_title_el] if selector == "h1" else []
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert result.success
    _patch_scrapling.Fetcher.get.assert_called_once()
    _patch_scrapling.StealthyFetcher.fetch.assert_not_called()


def test_rakuma_patrol_price_extraction(_patch_scrapling):
    """RakumaPatrol が価格を正しく抽出することを確認"""
    mock_title_el = MagicMock()
    mock_title_el.text = "テスト商品"

    mock_price_el = MagicMock()
    mock_price_el.text = "¥1,500"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥1,500 テスト商品"
    mock_page.css.side_effect = lambda selector: [mock_title_el] if selector == "h1" else [mock_price_el] if "price" in selector else []
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert result.price == 1500


def test_rakuma_patrol_sold_status(_patch_scrapling):
    """RakumaPatrol は sold selector を stale な InStock JSON-LD より優先する。"""
    sold_el = MagicMock()
    sold_el.text = "SOLDOUT"

    jsonld_el = MagicMock()
    jsonld_el.text = json.dumps(
        {
            "@context": "http://schema.org/",
            "@type": "Product",
            "name": "Rakuma Sold Item",
            "offers": {"@type": "Offer", "price": 5840, "availability": "http://schema.org/InStock"},
        }
    )

    mock_page = MagicMock()
    mock_page.get_text.return_value = "売り切れ"

    def css_side_effect(selector):
        if selector == "span.soldout":
            return [sold_el]
        if selector == "script[type='application/ld+json']":
            return [jsonld_el]
        return []

    mock_page.css.side_effect = css_side_effect
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert result.status == "sold"


def test_rakuma_patrol_error_handling(_patch_scrapling):
    """RakumaPatrol がエラーを正しく処理することを確認"""
    _patch_scrapling.Fetcher.get.side_effect = Exception("Network error")

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert not result.success
    assert "Network error" in result.error


def test_rakuma_patrol_driver_arg_ignored(_patch_scrapling):
    """RakumaPatrol が driver 引数を無視することを確認（後方互換性）"""
    mock_title_el = MagicMock()
    mock_title_el.text = "テスト商品"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥1,000 テスト商品"
    mock_page.css.side_effect = lambda selector: [mock_title_el] if selector == "h1" else []
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test", driver="fake_driver")

    # HTTP Fetcher.get が呼ばれたことを確認
    _patch_scrapling.Fetcher.get.assert_called_once()
    assert result.success


def test_fetch_rakuma_convenience_function(_patch_scrapling):
    """fetch_rakuma ヘルパー関数が正しく動作することを確認"""
    mock_title_el = MagicMock()
    mock_title_el.text = "テスト商品"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "テスト ¥500"
    mock_page.css.side_effect = lambda selector: [mock_title_el] if selector == "h1" else []
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import fetch_rakuma
    result = fetch_rakuma("https://item.fril.jp/test")

    assert result.success


def test_rakuma_patrol_maps_on_sale_to_active(_patch_scrapling):
    """RakumaPatrol が共有パーサの on_sale を patrol 用 active に変換することを確認"""
    mock_title_el = MagicMock()
    mock_title_el.text = "テスト商品"

    buy_el = MagicMock()
    buy_el.text = "購入に進む"

    mock_page = MagicMock()
    mock_page.get_text.return_value = "¥2,000 テスト商品"

    def css_side_effect(selector):
        if selector == "h1":
            return [mock_title_el]
        if selector in {"a.btn_buy", ".ga-item-buybutton", "a[href*='ref_action=btn_buy']"}:
            return [buy_el]
        return []

    mock_page.css.side_effect = css_side_effect
    mock_page.css_first.return_value = None

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/test")

    assert result.status == "active"


def test_rakuma_patrol_returns_deleted_on_404_without_alert(_patch_scrapling, monkeypatch):
    """RakumaPatrol は HTTP 404 を deleted に正規化し、patrol_error を出さない。"""
    events = []

    class FakeDispatcher:
        def notify_scrape_issue(self, **payload):
            events.append(payload)
            return True

    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: FakeDispatcher())

    mock_page = MagicMock()
    mock_page.status = 404
    mock_page.get_text.return_value = "ページが見つかりません"
    mock_page.css.return_value = []

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/missing")

    assert result.success
    assert result.status == "deleted"
    assert result.error is None
    assert result.reason == "http-404"
    assert events == []


def test_rakuma_patrol_returns_deleted_on_missing_item_marker_without_alert(_patch_scrapling, monkeypatch):
    """RakumaPatrol は missing marker を deleted に正規化し、patrol_error を出さない。"""
    events = []

    class FakeDispatcher:
        def notify_scrape_issue(self, **payload):
            events.append(payload)
            return True

    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: FakeDispatcher())

    mock_page = MagicMock()
    mock_page.get_text.return_value = "お探しの商品は見つかりません"
    mock_page.css.return_value = []

    _patch_scrapling.Fetcher.get.return_value = mock_page

    from services.patrol.rakuma_patrol import RakumaPatrol
    patrol = RakumaPatrol()
    result = patrol.fetch("https://item.fril.jp/missing")

    assert result.success
    assert result.status == "deleted"
    assert result.error is None
    assert result.reason == "missing-item-marker"
    assert events == []


# ---------------------------------------------------------------------------
# BROWSER_SITES classification
# ---------------------------------------------------------------------------

def test_rakuma_removed_from_browser_sites():
    """Stage 3 完了: rakuma / mercari ともに BROWSER_SITES から削除済みであることを確認"""
    from services.scrape_queue import BROWSER_SITES
    assert "rakuma" not in BROWSER_SITES
    assert "mercari" not in BROWSER_SITES


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
