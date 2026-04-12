"""
Self-healing scraper engine.

Automatically detects broken CSS selectors, rediscovers elements using
fingerprint-based scoring, generates new selectors, and persists fixes
to config/scraping_selectors.json for subsequent fast-path scraping.

Supports three parser backends:
  - Scrapling Selector (page.css)      → mercari_db, yahoo_db, snkrdunk_db
  - BeautifulSoup (soup.select)        → surugaya_db
  - Playwright Page (page.query_selector_all) → mercari_db Shops

Usage:
    from services.selector_healer import get_healer
    healer = get_healer()
    value, was_healed = healer.extract_with_healing(
        page, 'yahoo', 'detail', 'title', parser='scrapling'
    )
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from services.alerts import get_alert_dispatcher
from services.page_state_classifier import classify_page_state
from services.repair_store import record_repair_candidate

logger = logging.getLogger("selector_healer")

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
_SELECTORS_PATH = os.path.join(_CONFIG_DIR, "scraping_selectors.json")
_FINGERPRINTS_PATH = os.path.join(_CONFIG_DIR, "element_fingerprints.json")
_HEAL_LOG_PATH = os.path.join(_CONFIG_DIR, "heal_history.jsonl")

# ---------------------------------------------------------------------------
# Fingerprint & Selector JSON loaders
# ---------------------------------------------------------------------------

_fingerprints_cache: Optional[dict] = None
_fp_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_fingerprints(force: bool = False) -> dict:
    global _fingerprints_cache
    with _fp_lock:
        if _fingerprints_cache is not None and not force:
            return _fingerprints_cache
        try:
            with open(_FINGERPRINTS_PATH, "r", encoding="utf-8") as f:
                _fingerprints_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning("Failed to load fingerprints: %s", e)
            _fingerprints_cache = {}
        return _fingerprints_cache


def _load_selectors() -> dict:
    try:
        with open(_SELECTORS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_selectors(data: dict) -> None:
    with open(_SELECTORS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    # Invalidate selector_config cache so next load picks up changes
    try:
        from selector_config import load_selectors
        load_selectors(force_reload=True)
    except Exception:
        pass


def _assess_page_state(site: str, page, page_type: str):
    try:
        return classify_page_state(site, page, page_type=page_type)
    except Exception as exc:
        logger.warning("[%s/%s] Page-state classification failed, allowing legacy healing path: %s", site, page_type, exc)
        return None


def evaluate_selector_candidate(
    page,
    site: str,
    page_type: str,
    field: str,
    selector: str,
    *,
    parser: str = "scrapling",
) -> dict[str, Any]:
    adapter = _get_adapter(parser)
    fp_config = get_healer()._get_field_config(site, page_type, field)
    validators = fp_config.get("validators", {})
    elements = adapter.query_all(page, str(selector or "").strip())

    if field == "images":
        urls = []
        for element in elements:
            src = adapter.get_attr(element, "src") or adapter.get_attr(element, "data-src")
            if src and _validate_url(src, validators) and src not in urls:
                urls.append(src)
        return {
            "ok": bool(urls),
            "value": urls,
            "match_count": len(elements),
        }

    value = ""
    if elements:
        value = adapter.get_text(elements[0])

    return {
        "ok": bool(value and _validate_text(value, validators)),
        "value": value,
        "match_count": len(elements),
    }


# ---------------------------------------------------------------------------
# Parser adapters — unified interface across Scrapling / BS4 / Playwright
# ---------------------------------------------------------------------------

class _ScraplingAdapter:
    """Adapter for Scrapling Selector objects."""

    @staticmethod
    def query_all(page, selector: str) -> list:
        try:
            return list(page.css(selector))
        except Exception:
            return []

    @staticmethod
    def get_text(element) -> str:
        try:
            return str(element.text or "").strip()
        except Exception:
            return ""

    @staticmethod
    def get_all_text(element) -> str:
        try:
            return str(element.get_all_text(separator=" ", strip=True) or "")
        except Exception:
            return ""

    @staticmethod
    def get_attr(element, attr: str) -> str:
        try:
            return str(element.attrib.get(attr, "") or "")
        except Exception:
            return ""

    @staticmethod
    def get_tag(element) -> str:
        try:
            return str(element.tag or "").lower()
        except Exception:
            return ""

    @staticmethod
    def get_classes(element) -> List[str]:
        try:
            return str(element.attrib.get("class", "") or "").split()
        except Exception:
            return []

    @staticmethod
    def get_parent_tag(element) -> str:
        try:
            p = element.parent
            return str(p.tag or "").lower() if p else ""
        except Exception:
            return ""

    @staticmethod
    def generate_selector(element) -> str:
        try:
            return element.generate_css_selector
        except Exception:
            return ""

    @staticmethod
    def get_all_elements(page) -> list:
        """Return all elements in the page for fingerprint scanning."""
        try:
            return list(page.css("*"))
        except Exception:
            return []


class _BeautifulSoupAdapter:
    """Adapter for BeautifulSoup Tag objects."""

    @staticmethod
    def query_all(soup, selector: str) -> list:
        try:
            return soup.select(selector)
        except Exception:
            return []

    @staticmethod
    def get_text(element) -> str:
        try:
            return element.get_text(strip=True)
        except Exception:
            return ""

    @staticmethod
    def get_all_text(element) -> str:
        try:
            return element.get_text(" ", strip=True)
        except Exception:
            return ""

    @staticmethod
    def get_attr(element, attr: str) -> str:
        try:
            val = element.get(attr, "")
            if isinstance(val, list):
                return " ".join(val)
            return str(val or "")
        except Exception:
            return ""

    @staticmethod
    def get_tag(element) -> str:
        try:
            return str(element.name or "").lower()
        except Exception:
            return ""

    @staticmethod
    def get_classes(element) -> List[str]:
        try:
            cls = element.get("class", [])
            return cls if isinstance(cls, list) else str(cls).split()
        except Exception:
            return []

    @staticmethod
    def get_parent_tag(element) -> str:
        try:
            p = element.parent
            return str(p.name or "").lower() if p else ""
        except Exception:
            return ""

    @staticmethod
    def generate_selector(element) -> str:
        """Generate a CSS selector for a BS4 element."""
        try:
            parts = []
            el = element
            while el and getattr(el, "name", None) and el.name != "[document]":
                tag = el.name
                el_id = el.get("id")
                if el_id:
                    parts.append(f"#{el_id}")
                    break
                # Check for unique data-testid
                testid = el.get("data-testid")
                if testid:
                    parts.append(f"[data-testid='{testid}']")
                    break
                # Use tag + nth-of-type
                siblings = [s for s in el.parent.children
                            if getattr(s, "name", None) == tag] if el.parent else []
                if len(siblings) > 1:
                    idx = siblings.index(el) + 1
                    parts.append(f"{tag}:nth-of-type({idx})")
                else:
                    parts.append(tag)
                el = el.parent
            return " > ".join(reversed(parts))
        except Exception:
            return ""

    @staticmethod
    def get_all_elements(soup) -> list:
        try:
            return soup.find_all(True)
        except Exception:
            return []


def _get_adapter(parser: str):
    if parser == "scrapling":
        return _ScraplingAdapter
    elif parser == "bs4":
        return _BeautifulSoupAdapter
    else:
        raise ValueError(f"Unknown parser type: {parser}")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _validate_text(text: str, validators: dict) -> bool:
    """Check if extracted text passes validation rules."""
    if not validators:
        return bool(text)

    if "min_length" in validators and len(text) < validators["min_length"]:
        return False
    if "max_length" in validators and len(text) > validators["max_length"]:
        return False
    if "must_match" in validators and not re.search(validators["must_match"], text):
        return False
    if "must_not_contain" in validators:
        for bad in validators["must_not_contain"]:
            if bad in text:
                return False
    if "numeric_range" in validators:
        digits = re.sub(r"[^\d]", "", text)
        if digits:
            val = int(digits)
            lo, hi = validators["numeric_range"]
            if val < lo or val > hi:
                return False
    return True


def _validate_url(url: str, validators: dict) -> bool:
    """Check if extracted URL passes validation."""
    if not url:
        return False
    if "url_pattern" in validators:
        return bool(re.match(validators["url_pattern"], url))
    return url.startswith("http")


# ---------------------------------------------------------------------------
# Fingerprint-based element rediscovery
# ---------------------------------------------------------------------------

def _score_element(element, fingerprint: dict, adapter, field_type: str) -> float:
    """
    Score how well an element matches the expected fingerprint.
    Returns a score from 0 to 100.
    """
    score = 0.0
    checks = 0

    # 1. Tag name match (weight: 20)
    tag = adapter.get_tag(element)
    expected_tags = fingerprint.get("tag_names", [])
    if expected_tags:
        checks += 1
        if tag in expected_tags:
            score += 20

    # 2. Text pattern match (weight: 30) — for non-image fields
    if field_type != "images":
        text = adapter.get_text(element)
        text_pattern = fingerprint.get("text_pattern")
        if text_pattern:
            checks += 1
            if text and re.search(text_pattern, text):
                score += 30
    else:
        # For images, check src pattern
        src = adapter.get_attr(element, "src") or adapter.get_attr(element, "data-src")
        src_pattern = fingerprint.get("src_pattern")
        if src_pattern:
            checks += 1
            if src and re.search(src_pattern, src):
                score += 30

    # 3. Hint attribute match (weight: 25)
    hint_attrs = fingerprint.get("hint_attributes", {})
    if hint_attrs:
        checks += 1
        attr_score = 0
        for attr_name, attr_val in hint_attrs.items():
            actual = adapter.get_attr(element, attr_name)
            if actual == attr_val:
                attr_score += 25
            elif attr_val in actual:
                attr_score += 15
        score += min(attr_score, 25)

    # 4. Class hint match (weight: 15)
    class_hints = fingerprint.get("class_hints", [])
    if class_hints:
        checks += 1
        classes = adapter.get_classes(element)
        class_str = " ".join(classes)
        matched = sum(1 for hint in class_hints if hint in class_str)
        if matched:
            score += min(15, matched * 8)

    # 5. Content sanity check (weight: 10)
    checks += 1
    if field_type != "images":
        text = adapter.get_text(element)
        if text and len(text) >= 2:
            score += 10
    else:
        src = adapter.get_attr(element, "src") or adapter.get_attr(element, "data-src")
        if src and src.startswith("http"):
            score += 10

    if checks == 0:
        return 0
    return (score / (checks * (100 / checks))) * 100 if checks else 0


def _rediscover_element(page, fingerprint: dict, adapter, field_type: str,
                         validators: dict) -> Tuple[Optional[Any], float]:
    """
    Scan all elements in the page and find the best match for the fingerprint.
    Returns (element, score) or (None, 0).
    """
    tag_names = fingerprint.get("tag_names", ["*"])
    candidates = []

    # Pre-filter by tag name for efficiency
    for tag in tag_names:
        candidates.extend(adapter.query_all(page, tag))

    if not candidates:
        # Fallback: scan all elements
        candidates = adapter.get_all_elements(page)

    scored = []
    for el in candidates:
        s = _score_element(el, fingerprint, adapter, field_type)
        if s < 30:
            continue

        # Additional validation
        if field_type != "images":
            text = adapter.get_text(el)
            if text and _validate_text(text, validators):
                scored.append((s, el))
        else:
            src = adapter.get_attr(el, "src") or adapter.get_attr(el, "data-src")
            if src and _validate_url(src, validators):
                scored.append((s, el))

    if not scored:
        return None, 0

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1], scored[0][0]


# ---------------------------------------------------------------------------
# Heal log — append-only JSONL for tracking healing events
# ---------------------------------------------------------------------------

def _log_heal_event(site: str, page_type: str, field: str,
                    old_selector: str, new_selector: str, score: float) -> None:
    entry = {
        "timestamp": datetime.now().isoformat(),
        "site": site,
        "page_type": page_type,
        "field": field,
        "old_selector": old_selector,
        "new_selector": new_selector,
        "match_score": round(score, 2),
    }
    try:
        with open(_HEAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("Failed to write heal log: %s", e)


# ---------------------------------------------------------------------------
# SelectorHealer — main public class
# ---------------------------------------------------------------------------

class SelectorHealer:
    """
    Self-healing CSS selector engine.

    Wraps the normal selector iteration with:
      1. Validation of extracted values
      2. Fingerprint-based element rediscovery on failure
      3. Automatic selector generation and persistence
    """

    def __init__(self):
        self._fingerprints = _load_fingerprints()
        self._write_lock = threading.Lock()
        # In-memory cache: healed selectors survive even if file write fails
        # Key: (site, page_type, field) → new_selector string
        self._healed_selectors: Dict[tuple, str] = {}
        self._low_confidence_hits: Dict[tuple, int] = {}

    def reload(self) -> None:
        """Force-reload fingerprint data."""
        self._fingerprints = _load_fingerprints(force=True)

    # ----- public API -----

    def extract_with_healing(
        self,
        page,
        site: str,
        page_type: str,
        field: str,
        parser: str = "scrapling",
    ) -> Tuple[str, bool]:
        """
        Extract a field value with self-healing capability.

        1. Try each selector in order (normal path)
        2. Validate the result
        3. On failure → fingerprint-based rediscovery → generate new selector → save

        Args:
            page: The parsed page (Scrapling Selector / BS4 soup)
            site: Site key (mercari, yahoo, snkrdunk, rakuma, surugaya)
            page_type: Page type key (detail, search, shops, general)
            field: Field key (title, price, description, images)
            parser: Parser backend ('scrapling' or 'bs4')

        Returns:
            (value, was_healed): The extracted value and whether healing occurred.
        """
        adapter = _get_adapter(parser)
        fp_config = self._get_field_config(site, page_type, field)
        validators = fp_config.get("validators", {})
        fingerprint = fp_config.get("fingerprint", {})

        # Load selectors from config
        from selector_config import get_selectors
        selectors = get_selectors(site, page_type, field) or []

        # --- Phase 1: Normal selector path ---
        # Check in-memory healed selectors first (survives even when file write fails)
        cache_key = (site, page_type, field)
        healed_sel = self._healed_selectors.get(cache_key)
        check_selectors = [healed_sel] + selectors if healed_sel else selectors

        for selector in check_selectors:
            elements = adapter.query_all(page, selector)
            if not elements:
                continue

            if field == "images":
                # For images, collect all matching URLs
                urls = []
                for el in elements:
                    src = adapter.get_attr(el, "src") or adapter.get_attr(el, "data-src")
                    if src and _validate_url(src, validators):
                        urls.append(src)
                if urls:
                    self._low_confidence_hits.pop(cache_key, None)
                    return json.dumps(urls), False
            else:
                text = adapter.get_text(elements[0])
                if text and _validate_text(text, validators):
                    self._low_confidence_hits.pop(cache_key, None)
                    return text, False

        logger.info("[%s/%s/%s] All %d selectors failed, attempting self-healing...",
                    site, page_type, field, len(selectors))

        assessment = _assess_page_state(site, page, page_type)
        if assessment is not None and not assessment.allow_healing:
            self._notify_selector_issue(
                event_type="heal_skipped",
                site=site,
                page_type=page_type,
                field=field,
                severity="warning",
                message="Self-healing was skipped because the page state was not classified as healthy.",
                details={
                    "reason": "page_state_disallowed",
                    "page_state": assessment.state,
                    "page_state_reasons": list(assessment.reasons),
                    "selector_count": len(selectors),
                    "parser": parser,
                },
            )
            return "", False

        # --- Phase 2: Fingerprint-based rediscovery ---
        if not fingerprint:
            logger.debug("No fingerprint available for %s/%s/%s", site, page_type, field)
            self._notify_selector_issue(
                event_type="heal_failed",
                site=site,
                page_type=page_type,
                field=field,
                severity="error",
                message="Self-healing could not start because no fingerprint was available.",
                details={"reason": "missing_fingerprint", "selector_count": len(selectors), "parser": parser},
            )
            return "", False

        element, score = _rediscover_element(page, fingerprint, adapter, field, validators)
        if element is None:
            logger.warning("[%s/%s/%s] Self-healing failed: element not found (score too low)",
                          site, page_type, field)
            self._notify_selector_issue(
                event_type="heal_failed",
                site=site,
                page_type=page_type,
                field=field,
                severity="error",
                message="Self-healing failed because no sufficiently strong candidate was found.",
                details={"reason": "element_not_found", "selector_count": len(selectors), "parser": parser},
            )
            return "", False

        # Extract value from rediscovered element
        if field == "images":
            src = adapter.get_attr(element, "src") or adapter.get_attr(element, "data-src")
            value = json.dumps([src]) if src else ""
        else:
            value = adapter.get_text(element)

        if not value:
            self._notify_selector_issue(
                event_type="heal_failed",
                site=site,
                page_type=page_type,
                field=field,
                severity="error",
                message="Self-healing rediscovered an element but could not extract a usable value.",
                details={"reason": "empty_recovered_value", "score": round(score, 2), "parser": parser},
            )
            return "", False

        # --- Phase 3: Generate new selector and save ---
        new_selector = adapter.generate_selector(element)
        if new_selector:
            old_first = selectors[0] if selectors else "(none)"
            # Always cache in memory (even if file write fails)
            self._healed_selectors[(site, page_type, field)] = new_selector
            persisted = self._save_healed_selector(site, page_type, field, new_selector)
            _log_heal_event(site, page_type, field, old_first, new_selector, score)
            if persisted:
                logger.info(
                    "[%s/%s/%s] HEALED: new selector '%s' (score=%.1f) → saved",
                    site, page_type, field, new_selector, score,
                )
            else:
                logger.warning(
                    "[%s/%s/%s] HEALED in memory but selector persistence failed for '%s' (score=%.1f)",
                    site, page_type, field, new_selector, score,
                )
                self._notify_selector_issue(
                    event_type="persist_failed",
                    site=site,
                    page_type=page_type,
                    field=field,
                    severity="warning",
                    message="Selector healing succeeded in memory but could not be persisted.",
                    details={"reason": "selector_persist_failed", "new_selector": new_selector, "score": round(score, 2)},
                )
            self._record_repair_candidate(
                site=site,
                page_type=page_type,
                field=field,
                parser=parser,
                proposed_selector=new_selector,
                source_selector=old_first,
                score=score,
                details={
                    "persisted_to_json": persisted,
                    "page_url": str(getattr(page, "url", "") or ""),
                    "selector_count": len(selectors),
                },
            )
            self._record_low_confidence_hit(site, page_type, field, score, new_selector)
            return value, True
        else:
            logger.info(
                "[%s/%s/%s] Element rediscovered (score=%.1f) but could not generate selector",
                site, page_type, field, score,
            )
            return value, True

    def extract_images_with_healing(
        self,
        page,
        site: str,
        page_type: str,
        parser: str = "scrapling",
    ) -> Tuple[List[str], bool]:
        """
        Specialized image extraction with healing.
        Returns (list_of_urls, was_healed).
        """
        adapter = _get_adapter(parser)
        fp_config = self._get_field_config(site, page_type, "images")
        validators = fp_config.get("validators", {})
        fingerprint = fp_config.get("fingerprint", {})

        from selector_config import get_selectors
        selectors = get_selectors(site, page_type, "images") or []

        # --- Phase 1: Normal path ---
        for selector in selectors:
            elements = adapter.query_all(page, selector)
            urls = []
            for el in elements:
                src = adapter.get_attr(el, "src") or adapter.get_attr(el, "data-src")
                if src and _validate_url(src, validators):
                    if src not in urls:
                        urls.append(src)
            if urls:
                self._low_confidence_hits.pop((site, page_type, "images"), None)
                return urls, False

        logger.info("[%s/%s/images] All %d selectors failed, attempting self-healing...",
                    site, page_type, len(selectors))

        assessment = _assess_page_state(site, page, page_type)
        if assessment is not None and not assessment.allow_healing:
            self._notify_selector_issue(
                event_type="heal_skipped",
                site=site,
                page_type=page_type,
                field="images",
                severity="warning",
                message="Image healing was skipped because the page state was not classified as healthy.",
                details={
                    "reason": "page_state_disallowed",
                    "page_state": assessment.state,
                    "page_state_reasons": list(assessment.reasons),
                    "selector_count": len(selectors),
                    "parser": parser,
                },
            )
            return [], False

        # --- Phase 2: Fingerprint scan for all image elements ---
        if not fingerprint:
            self._notify_selector_issue(
                event_type="heal_failed",
                site=site,
                page_type=page_type,
                field="images",
                severity="error",
                message="Image healing could not start because no fingerprint was available.",
                details={"reason": "missing_fingerprint", "selector_count": len(selectors), "parser": parser},
            )
            return [], False

        tag_names = fingerprint.get("tag_names", ["img"])
        src_pattern = fingerprint.get("src_pattern", "")
        urls = []
        healed_el = None

        for tag in tag_names:
            for el in adapter.query_all(page, tag):
                src = adapter.get_attr(el, "src") or adapter.get_attr(el, "data-src")
                if not src or not src.startswith("http"):
                    continue
                if src_pattern and not re.search(src_pattern, src):
                    continue
                if src not in urls:
                    urls.append(src)
                    if healed_el is None:
                        healed_el = el

        if urls and healed_el:
            new_selector = adapter.generate_selector(healed_el)
            if new_selector:
                old_first = selectors[0] if selectors else "(none)"
                self._healed_selectors[(site, page_type, "images")] = new_selector
                persisted = self._save_healed_selector(site, page_type, "images", new_selector)
                _log_heal_event(site, page_type, "images", old_first, new_selector, 100)
                if persisted:
                    logger.info("[%s/%s/images] HEALED: new selector '%s' → saved",
                                site, page_type, new_selector)
                else:
                    logger.warning(
                        "[%s/%s/images] HEALED in memory but selector persistence failed for '%s'",
                        site, page_type, new_selector,
                    )
                    self._notify_selector_issue(
                        event_type="persist_failed",
                        site=site,
                        page_type=page_type,
                        field="images",
                        severity="warning",
                        message="Image healing succeeded in memory but could not be persisted.",
                        details={"reason": "selector_persist_failed", "new_selector": new_selector, "score": 100},
                    )
                self._record_repair_candidate(
                    site=site,
                    page_type=page_type,
                    field="images",
                    parser=parser,
                    proposed_selector=new_selector,
                    source_selector=old_first,
                    score=100,
                    details={
                        "persisted_to_json": persisted,
                        "page_url": str(getattr(page, "url", "") or ""),
                        "selector_count": len(selectors),
                    },
                )
            return urls, True

        self._notify_selector_issue(
            event_type="heal_failed",
            site=site,
            page_type=page_type,
            field="images",
            severity="error",
            message="Image healing failed because no matching image candidate was found.",
            details={"reason": "image_not_found", "selector_count": len(selectors), "parser": parser},
        )
        return [], False

    # ----- internal helpers -----

    def _get_field_config(self, site: str, page_type: str, field: str) -> dict:
        return (self._fingerprints
                .get(site, {})
                .get(page_type, {})
                .get(field, {}))

    def _notify_selector_issue(
        self,
        *,
        event_type: str,
        site: str,
        page_type: str,
        field: str,
        severity: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        try:
            get_alert_dispatcher().notify_selector_issue(
                event_type=event_type,
                site=site,
                page_type=page_type,
                field=field,
                severity=severity,
                message=message,
                details=details or {},
            )
        except Exception as exc:
            logger.debug("Selector alert dispatch hook failed: %s", exc)

    def _record_repair_candidate(
        self,
        *,
        site: str,
        page_type: str,
        field: str,
        parser: str,
        proposed_selector: str,
        source_selector: str,
        score: float | int,
        details: dict | None = None,
    ) -> int | None:
        candidate_id = record_repair_candidate(
            site=site,
            page_type=page_type,
            field=field,
            parser=parser,
            proposed_selector=proposed_selector,
            source_selector=source_selector,
            score=score,
            page_state="healthy",
            details=details,
        )
        if candidate_id is not None:
            numeric_score = None
            if score is not None:
                try:
                    numeric_score = round(float(score), 2)
                except (TypeError, ValueError):
                    numeric_score = None
            self._notify_selector_issue(
                event_type="repair_candidate_recorded",
                site=site,
                page_type=page_type,
                field=field,
                severity="warning",
                message="A selector repair candidate was recorded and is awaiting review.",
                details={
                    "candidate_id": candidate_id,
                    "score": numeric_score,
                    **(details or {}),
                },
            )
        return candidate_id

    def _record_low_confidence_hit(
        self,
        site: str,
        page_type: str,
        field: str,
        score: float,
        new_selector: str,
    ) -> None:
        threshold = _env_float("SELECTOR_ALERT_LOW_CONFIDENCE_THRESHOLD", 65.0)
        repeat_count = max(1, _env_int("SELECTOR_ALERT_LOW_CONFIDENCE_REPEAT", 3))
        key = (site, page_type, field)

        if score >= threshold:
            self._low_confidence_hits.pop(key, None)
            return

        hits = self._low_confidence_hits.get(key, 0) + 1
        self._low_confidence_hits[key] = hits
        if hits < repeat_count:
            return

        self._low_confidence_hits[key] = 0
        self._notify_selector_issue(
            event_type="low_confidence_repeated",
            site=site,
            page_type=page_type,
            field=field,
            severity="warning",
            message="Repeated low-confidence selector healing was detected.",
            details={
                "score": round(score, 2),
                "threshold": threshold,
                "repeat_count": repeat_count,
                "new_selector": new_selector,
            },
        )

    def _save_healed_selector(self, site: str, page_type: str, field: str,
                               new_selector: str) -> bool:
        """Insert the new selector at the top of the selector list in JSON.
        
        File write is best-effort — failures are logged but do NOT propagate.
        The in-memory cache (_healed_selectors) always has the healed selector
        regardless of whether the file write succeeds.
        """
        try:
            with self._write_lock:
                data = _load_selectors()
                if site not in data:
                    data[site] = {}
                if page_type not in data[site]:
                    data[site][page_type] = {}
                if field not in data[site][page_type]:
                    data[site][page_type][field] = []

                current = data[site][page_type][field]
                if new_selector in current:
                    # Move to front
                    current.remove(new_selector)
                current.insert(0, new_selector)

                # Update timestamp
                data["_updated"] = datetime.now().strftime("%Y-%m-%d")

                _save_selectors(data)
                return True
        except PermissionError:
            logger.warning(
                "[%s/%s/%s] Cannot write healed selector to JSON (permission denied). "
                "Healed selector '%s' is cached in memory only.",
                site, page_type, field, new_selector,
            )
            return False
        except Exception as e:
            logger.warning(
                "[%s/%s/%s] Failed to persist healed selector: %s",
                site, page_type, field, e,
            )
            return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_healer_instance: Optional[SelectorHealer] = None
_healer_lock = threading.Lock()


def get_healer() -> SelectorHealer:
    """Get or create the singleton SelectorHealer instance."""
    global _healer_instance
    if _healer_instance is None:
        with _healer_lock:
            if _healer_instance is None:
                _healer_instance = SelectorHealer()
    return _healer_instance
