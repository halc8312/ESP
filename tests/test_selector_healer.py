"""
Unit tests for the self-healing scraper engine (services/selector_healer.py).

Tests three scenarios:
  1. Normal extraction succeeds → no healing needed
  2. Selectors broken → fingerprint-based rediscovery finds element → new selector generated
  3. Fingerprint scan fails → returns empty (graceful degradation)
"""

import json
import os
import sys
import tempfile

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# Minimal mock objects that mimic Scrapling Selector API
# ---------------------------------------------------------------------------

class MockElement:
    """Simulates a Scrapling Selector element."""

    def __init__(self, tag: str, text: str = "", attribs: dict = None, parent=None):
        self._tag = tag
        self._text = text
        self.attrib = attribs or {}
        self._parent = parent

    @property
    def tag(self):
        return self._tag

    @property
    def text(self):
        return self._text

    @property
    def parent(self):
        return self._parent

    @property
    def generate_css_selector(self):
        if self.attrib.get("id"):
            return f"#{self.attrib['id']}"
        if self.attrib.get("data-testid"):
            return f"[data-testid='{self.attrib['data-testid']}']"
        cls = self.attrib.get("class", "")
        if cls:
            return f"{self._tag}.{cls.replace(' ', '.')}"
        return self._tag

    def get_all_text(self, separator=" ", strip=True):
        return self._text


class MockPage:
    """Simulates a Scrapling page with .css() method."""

    def __init__(self, elements_map: dict = None, all_elements: list = None):
        self._map = elements_map or {}
        self._all = all_elements or []

    def css(self, selector: str):
        return self._map.get(selector, [])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_config_dir():
    """Create temporary config files for testing."""
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="healer_test_")

    selectors = {
        "_updated": "2026-01-01",
        "testsite": {
            "detail": {
                "title": [
                    ".old-title-class",
                    "h1.legacy"
                ],
                "price": [
                    ".old-price"
                ]
            }
        }
    }

    fingerprints = {
        "testsite": {
            "detail": {
                "title": {
                    "validators": {
                        "min_length": 3,
                        "max_length": 200
                    },
                    "fingerprint": {
                        "tag_names": ["h1", "h2"],
                        "text_pattern": ".{3,200}",
                        "class_hints": ["product-title", "item-name"]
                    }
                },
                "price": {
                    "validators": {
                        "must_match": "[\\d,¥￥円]+",
                        "numeric_range": [1, 99999999]
                    },
                    "fingerprint": {
                        "tag_names": ["span", "p"],
                        "text_pattern": "[¥￥]\\s*[\\d,]+",
                        "class_hints": ["new-price"]
                    }
                }
            }
        }
    }

    config_dir = os.path.join(tmpdir, "config")
    os.makedirs(config_dir)

    with open(os.path.join(config_dir, "scraping_selectors.json"), "w") as f:
        json.dump(selectors, f)

    with open(os.path.join(config_dir, "element_fingerprints.json"), "w") as f:
        json.dump(fingerprints, f)

    yield config_dir

    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def healer_with_temp_config(temp_config_dir, monkeypatch):
    """Create a SelectorHealer pointing at temp config files."""
    import services.selector_healer as sh

    monkeypatch.setattr(sh, "_CONFIG_DIR", str(temp_config_dir))
    monkeypatch.setattr(sh, "_SELECTORS_PATH", os.path.join(temp_config_dir, "scraping_selectors.json"))
    monkeypatch.setattr(sh, "_FINGERPRINTS_PATH", os.path.join(temp_config_dir, "element_fingerprints.json"))
    monkeypatch.setattr(sh, "_HEAL_LOG_PATH", os.path.join(temp_config_dir, "heal_history.jsonl"))

    # Reset caches
    sh._fingerprints_cache = None
    sh._healer_instance = None

    healer = sh.SelectorHealer()
    return healer, temp_config_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestValidators:
    """Test validation functions."""

    def test_validate_text_min_length(self):
        from services.selector_healer import _validate_text
        assert _validate_text("ab", {"min_length": 3}) is False
        assert _validate_text("abc", {"min_length": 3}) is True

    def test_validate_text_max_length(self):
        from services.selector_healer import _validate_text
        assert _validate_text("a" * 501, {"max_length": 500}) is False
        assert _validate_text("a" * 500, {"max_length": 500}) is True

    def test_validate_text_must_match(self):
        from services.selector_healer import _validate_text
        assert _validate_text("¥1,200", {"must_match": r"[¥￥\d,]+"}) is True
        assert _validate_text("no price", {"must_match": r"[¥￥\d,]+"}) is False

    def test_validate_text_must_not_contain(self):
        from services.selector_healer import _validate_text
        assert _validate_text("<!DOCTYPE html>", {"must_not_contain": ["<!DOCTYPE"]}) is False
        assert _validate_text("Normal title", {"must_not_contain": ["<!DOCTYPE"]}) is True

    def test_validate_text_numeric_range(self):
        from services.selector_healer import _validate_text
        assert _validate_text("¥1,200", {"numeric_range": [1, 99999999]}) is True
        assert _validate_text("¥0", {"numeric_range": [1, 99999999]}) is False

    def test_validate_url(self):
        from services.selector_healer import _validate_url
        assert _validate_url("https://example.com/img.jpg", {"url_pattern": "^https?://"}) is True
        assert _validate_url("data:image/png", {"url_pattern": "^https?://"}) is False
        assert _validate_url("", {}) is False


class TestScoring:
    """Test fingerprint scoring."""

    def test_score_high_when_matching(self):
        from services.selector_healer import _score_element, _ScraplingAdapter

        el = MockElement(
            tag="h1",
            text="Great Product Title Here",
            attribs={"class": "new-product-title"},
        )
        fingerprint = {
            "tag_names": ["h1"],
            "text_pattern": ".{3,200}",
            "class_hints": ["product-title"],
        }
        score = _score_element(el, fingerprint, _ScraplingAdapter, "title")
        assert score > 50, f"Expected score > 50, got {score}"

    def test_score_low_when_not_matching(self):
        from services.selector_healer import _score_element, _ScraplingAdapter

        el = MockElement(tag="div", text="", attribs={})
        fingerprint = {
            "tag_names": ["h1"],
            "text_pattern": ".{3,200}",
            "class_hints": ["product-title"],
        }
        score = _score_element(el, fingerprint, _ScraplingAdapter, "title")
        assert score < 30, f"Expected score < 30, got {score}"


class TestSelectorHealer:
    """Integration tests for SelectorHealer."""

    def test_normal_extraction_no_healing(self, healer_with_temp_config, monkeypatch):
        """When selectors work normally, no healing should occur."""
        healer, config_dir = healer_with_temp_config

        title_el = MockElement(tag="h1", text="Working Title")
        page = MockPage(elements_map={".old-title-class": [title_el]})

        # Mock get_selectors to return our test selectors
        monkeypatch.setattr(
            "services.selector_healer.get_healer",
            lambda: healer,
        )
        import selector_config
        monkeypatch.setattr(
            selector_config, "_selectors_cache",
            json.load(open(os.path.join(config_dir, "scraping_selectors.json")))
        )

        value, was_healed = healer.extract_with_healing(
            page, "testsite", "detail", "title", parser="scrapling"
        )
        assert value == "Working Title"
        assert was_healed is False

    def test_healing_when_selectors_broken(self, healer_with_temp_config, monkeypatch):
        """When all selectors fail but fingerprint matches, healing should occur."""
        healer, config_dir = healer_with_temp_config

        # The good element that fingerprint should discover
        good_el = MockElement(
            tag="h1",
            text="Product Found via Healing",
            attribs={"class": "new-product-title", "id": "healed-title"},
        )

        # Page where no existing selectors work, but h1 query returns our element
        page = MockPage(
            elements_map={
                ".old-title-class": [],  # broken
                "h1.legacy": [],         # broken
                "h1": [good_el],         # fingerprint scan will find this
                "h2": [],
                "*": [good_el],
            }
        )

        import selector_config
        monkeypatch.setattr(
            selector_config, "_selectors_cache",
            json.load(open(os.path.join(config_dir, "scraping_selectors.json")))
        )

        value, was_healed = healer.extract_with_healing(
            page, "testsite", "detail", "title", parser="scrapling"
        )
        assert value == "Product Found via Healing"
        assert was_healed is True

        # Verify the new selector was saved to the JSON file
        with open(os.path.join(config_dir, "scraping_selectors.json")) as f:
            updated = json.load(f)
        saved_selectors = updated["testsite"]["detail"]["title"]
        assert saved_selectors[0] == "#healed-title", f"Expected healed selector at front, got {saved_selectors}"

    def test_graceful_degradation_when_nothing_matches(self, healer_with_temp_config, monkeypatch):
        """When both selectors and fingerprint fail, should return empty gracefully."""
        healer, config_dir = healer_with_temp_config

        # Page with no matching elements at all
        page = MockPage(
            elements_map={
                ".old-title-class": [],
                "h1.legacy": [],
                "h1": [],
                "h2": [],
                "*": [],
            }
        )

        import selector_config
        monkeypatch.setattr(
            selector_config, "_selectors_cache",
            json.load(open(os.path.join(config_dir, "scraping_selectors.json")))
        )

        value, was_healed = healer.extract_with_healing(
            page, "testsite", "detail", "title", parser="scrapling"
        )
        assert value == ""
        assert was_healed is False


class TestHealLog:
    """Test heal event logging."""

    def test_heal_log_written(self, healer_with_temp_config, monkeypatch):
        """Verify heal events are logged to JSONL file."""
        healer, config_dir = healer_with_temp_config

        good_el = MockElement(
            tag="span",
            text="¥12,800",
            attribs={"class": "new-price", "data-testid": "item-price"},
        )

        page = MockPage(
            elements_map={
                ".old-price": [],  # broken
                "span": [good_el],
                "p": [],
                "*": [good_el],
            }
        )

        import selector_config
        monkeypatch.setattr(
            selector_config, "_selectors_cache",
            json.load(open(os.path.join(config_dir, "scraping_selectors.json")))
        )

        healer.extract_with_healing(
            page, "testsite", "detail", "price", parser="scrapling"
        )

        log_path = os.path.join(config_dir, "heal_history.jsonl")
        assert os.path.exists(log_path), "Heal log should be created"
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["site"] == "testsite"
        assert entry["field"] == "price"
        assert "new_selector" in entry
