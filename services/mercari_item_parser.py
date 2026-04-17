"""
Shared Mercari item-detail parsing helpers.

The parser classifies a page before extracting price so generic shell content
cannot be mistaken for product price data.
"""
import json
import logging
import re
from html import unescape
from typing import Optional

from services.detail_field_strategy_runner import DetailFieldStrategy, run_detail_field_strategies
from services.extraction_policy import attach_extraction_trace
from selector_config import get_selectors

logger = logging.getLogger("mercari.parser")

_MISSING_PAGE_MARKERS = (
    "該当する商品は削除されています",
    "この商品は削除されています",
    "お探しの商品は見つかりません",
    "商品は存在しません",
)
_HOME_TITLES = {
    "メルカリ - 日本最大のフリマサービス",
    "Mercari: Your Marketplace",
}
_ACTIVE_BUTTON_MARKERS = ("購入手続きへ", "購入する", "Buy this item")
_SOLD_MARKERS = ("売り切れ", "売り切れました", "SOLD")
_DEFAULT_CHECKOUT_BUTTON_SELECTORS = ["[data-testid='checkout-button']"]
_DEFAULT_STATUS_BADGE_SELECTORS = ["[data-testid='sold-out-badge']"]
_PRODUCT_IMAGE_FALLBACK = [
    "img[src*='static.mercdn.net'][src*='/item/'][src*='/photos/']",
    "img[data-src*='static.mercdn.net'][data-src*='/item/'][data-src*='/photos/']",
    "img[data-lazy*='static.mercdn.net'][data-lazy*='/item/'][data-lazy*='/photos/']",
    "img[data-lazy-src*='static.mercdn.net'][data-lazy-src*='/item/'][data-lazy-src*='/photos/']",
    "img[srcset*='static.mercdn.net'][srcset*='/item/'][srcset*='/photos/']",
    "source[srcset*='static.mercdn.net'][srcset*='/item/'][srcset*='/photos/']",
]
_PLACEHOLDER_IMAGE_MARKERS = ("data:image", "placeholder", "blank", "transparent", "pixel")
_PRODUCT_IMAGE_URL_PATTERN = re.compile(
    r"https?://static\.mercdn\.net/item/[^\"'\s<>]*?/photos/[^\"'\s<>]+",
    re.IGNORECASE,
)


def _is_nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_price(value) -> bool:
    return isinstance(value, int) and value > 0


def _has_image_urls(value) -> bool:
    return isinstance(value, list) and len(value) > 0


def _empty_item(url: str, status: str = "unknown") -> dict:
    return {
        "url": url,
        "title": "",
        "price": None,
        "status": status,
        "description": "",
        "image_urls": [],
        "variants": [],
    }


def _safe_css(page, selector: str) -> list:
    try:
        return list(page.css(selector))
    except Exception:
        return []


def _node_text(node) -> str:
    text = getattr(node, "text", "")
    return text.strip() if isinstance(text, str) else ""


def _node_attr(node, attr: str) -> str:
    attrib = getattr(node, "attrib", {}) or {}
    value = attrib.get(attr, "")
    return value.strip() if isinstance(value, str) else ""


def _normalize_image_url(url: str) -> str:
    normalized = url.strip().strip("'\"") if isinstance(url, str) else ""
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    return normalized


def _is_placeholder_image_url(url: str) -> bool:
    normalized = (url or "").lower()
    if normalized.startswith("data:"):
        return True
    return any(marker in normalized for marker in _PLACEHOLDER_IMAGE_MARKERS)


def _is_mercari_product_image_url(url: str) -> bool:
    normalized = (url or "").lower()
    return (
        normalized.startswith("http")
        and "mercdn.net" in normalized
        and "/item/" in normalized
        and "/photos/" in normalized
    )


def _append_unique_image_url(image_urls: list, url: str) -> None:
    normalized = _normalize_image_url(url)
    if not normalized or _is_placeholder_image_url(normalized):
        return
    if not _is_mercari_product_image_url(normalized):
        return
    if normalized not in image_urls:
        image_urls.append(normalized)


def _parse_srcset_urls(raw_value: str) -> list:
    urls = []
    if not isinstance(raw_value, str):
        return urls

    for candidate in raw_value.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        normalized = _normalize_image_url(parts[0])
        if normalized and normalized not in urls:
            urls.append(normalized)
    return urls


def _extract_node_image_url(node) -> str:
    for attr in ("data-src", "data-lazy", "data-lazy-src", "data-original", "data-image", "data-image-url"):
        candidate = _normalize_image_url(_node_attr(node, attr))
        if candidate and not _is_placeholder_image_url(candidate) and _is_mercari_product_image_url(candidate):
            return candidate

    for attr in ("srcset", "data-srcset"):
        srcset_urls = _parse_srcset_urls(_node_attr(node, attr))
        for candidate in reversed(srcset_urls):
            if not _is_placeholder_image_url(candidate) and _is_mercari_product_image_url(candidate):
                return candidate

    candidate = _normalize_image_url(_node_attr(node, "src"))
    if candidate and not _is_placeholder_image_url(candidate) and _is_mercari_product_image_url(candidate):
        return candidate

    return ""


def _extract_page_title(page) -> str:
    title_nodes = _safe_css(page, "title")
    if title_nodes:
        return _node_text(title_nodes[0])
    return ""


def _extract_body_text(page) -> str:
    """Extract body text, preferring narrow product-detail containers first.

    The old implementation concatenated *every* element under ``body *`` which
    pulled in unrelated UI chrome, hidden elements, and footer text — a major
    source of false-positive "sold" signals.  We now try progressively wider
    scopes so that product-area text is checked first.
    """
    # --- Narrow scopes: product detail containers and CTA regions ---
    _NARROW_SELECTORS = (
        "main",
        "article",
        "[data-testid='item-info']",
        "[data-testid='product-info']",
        "[id='item-info']",
        "section",
    )
    for selector in _NARROW_SELECTORS:
        parts = []
        for node in _safe_css(page, selector):
            # Skip hidden elements that may contain stale sold badges
            aria_hidden = _node_attr(node, "aria-hidden").lower()
            if aria_hidden == "true":
                continue
            text = _node_text(node)
            if text:
                parts.append(text)
        if parts:
            return " ".join(parts)

    # --- Fallback: direct children of body only (avoids deep hidden nodes) ---
    body_children = _safe_css(page, "body > *")
    if body_children:
        parts = []
        for node in body_children:
            aria_hidden = _node_attr(node, "aria-hidden").lower()
            if aria_hidden == "true":
                continue
            text = _node_text(node)
            if text:
                parts.append(text)
        if parts:
            return " ".join(parts)

    # --- Last resort: helper methods on the page object ---
    for attr_name in ("get_all_text", "get_text"):
        extractor = getattr(page, attr_name, None)
        if not callable(extractor):
            continue
        try:
            text = extractor() or ""
        except Exception:
            continue
        if isinstance(text, str) and text.strip():
            return text.strip()

    body_nodes = _safe_css(page, "body")
    if body_nodes:
        return _node_text(body_nodes[0])

    return ""


def _extract_price_from_text(text: str) -> Optional[int]:
    if not text:
        return None

    for pattern in (r"[¥￥]\s*([\d,]+)", r"([\d,]+)\s*円"):
        match = re.search(pattern, text)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group(1) or "")
        if not digits:
            continue
        try:
            return int(digits)
        except ValueError:
            continue
    return None


def _extract_plain_number(text: str) -> Optional[int]:
    if not text:
        return None

    match = re.search(r"\d[\d,]*", text)
    if not match:
        return None

    digits = re.sub(r"\D", "", match.group(0) or "")
    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def _extract_meta_price(page) -> Optional[int]:
    meta_nodes = _safe_css(page, "meta[name='product:price:amount']")
    if not meta_nodes:
        return None

    raw = _node_attr(meta_nodes[0], "content")
    if not raw:
        return None

    try:
        return int(float(raw.replace(",", "")))
    except ValueError:
        return _extract_price_from_text(raw)


def _find_product_jsonld(node):
    if isinstance(node, list):
        for item in node:
            found = _find_product_jsonld(item)
            if found:
                return found
        return None

    if isinstance(node, dict):
        node_type = node.get("@type")
        if isinstance(node_type, list):
            types = [str(value).lower() for value in node_type]
        else:
            types = [str(node_type).lower()] if node_type else []
        if "product" in types:
            return node

        for key in ("@graph", "mainEntity", "itemListElement"):
            found = _find_product_jsonld(node.get(key))
            if found:
                return found

    return None


def _extract_product_jsonld(page) -> Optional[dict]:
    for script in _safe_css(page, "script[type='application/ld+json']"):
        raw = _node_text(script)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        product = _find_product_jsonld(payload)
        if product:
            return product
    return None


def _extract_jsonld_price(product: Optional[dict]) -> Optional[int]:
    if not isinstance(product, dict):
        return None

    offers = product.get("offers")
    if isinstance(offers, dict):
        for key in ("price", "lowPrice", "highPrice"):
            price = offers.get(key)
            if price is None:
                continue
            parsed = _extract_plain_number(str(price))
            if parsed is not None:
                return parsed
        nested = offers.get("offers")
        if nested:
            return _extract_jsonld_price({"offers": nested})
    elif isinstance(offers, list):
        for offer in offers:
            parsed = _extract_jsonld_price({"offers": offer})
            if parsed is not None:
                return parsed
    return None


def _extract_jsonld_availability(product: Optional[dict]) -> str:
    if not isinstance(product, dict):
        return ""

    offers = product.get("offers")
    if isinstance(offers, dict):
        availability = offers.get("availability")
        if isinstance(availability, str) and availability:
            return availability.lower()
        nested = offers.get("offers")
        if nested:
            return _extract_jsonld_availability({"offers": nested})
    elif isinstance(offers, list):
        for offer in offers:
            availability = _extract_jsonld_availability({"offers": offer})
            if availability:
                return availability
    return ""


def _collect_jsonld_images(product_jsonld: Optional[dict]) -> list:
    if not isinstance(product_jsonld, dict):
        return []

    image_urls = []
    raw_images = product_jsonld.get("image")
    if isinstance(raw_images, str):
        _append_unique_image_url(image_urls, raw_images)
    elif isinstance(raw_images, dict):
        for key in ("url", "contentUrl", "image", "src"):
            raw_url = raw_images.get(key)
            if isinstance(raw_url, str):
                _append_unique_image_url(image_urls, raw_url)
    elif isinstance(raw_images, list):
        for image in raw_images:
            if isinstance(image, str):
                _append_unique_image_url(image_urls, image)
            elif isinstance(image, dict):
                for key in ("url", "contentUrl", "image", "src"):
                    raw_url = image.get(key)
                    if isinstance(raw_url, str):
                        _append_unique_image_url(image_urls, raw_url)
                        break
    return image_urls


def _extract_meta_image(page) -> list:
    image_urls = []
    for selector in ("meta[property='og:image']", "meta[name='twitter:image']"):
        for node in _safe_css(page, selector):
            _append_unique_image_url(image_urls, _node_attr(node, "content"))
        if image_urls:
            return image_urls
    return image_urls


def _extract_raw_html(page) -> str:
    raw_html = getattr(page, "body", "")
    if isinstance(raw_html, bytes):
        return raw_html.decode("utf-8", errors="ignore")
    if isinstance(raw_html, str):
        return raw_html
    return ""


def _extract_embedded_html_images(page) -> list:
    raw_html = _extract_raw_html(page)
    if not raw_html:
        return []

    normalized_html = unescape(raw_html).replace("\\/", "/")
    image_urls = []
    for match in _PRODUCT_IMAGE_URL_PATTERN.findall(normalized_html):
        _append_unique_image_url(image_urls, match)
    return image_urls


def _merge_image_sources(*sources: tuple[str, list]) -> tuple[list, str]:
    merged_urls = []
    contributing_sources = []

    for source_name, urls in sources:
        if not isinstance(urls, list):
            continue

        before_count = len(merged_urls)
        for url in urls:
            _append_unique_image_url(merged_urls, url)

        if len(merged_urls) > before_count:
            contributing_sources.append(source_name)

    return merged_urls, "+".join(contributing_sources)


def _extract_title(page, product_jsonld: Optional[dict]) -> str:
    selectors = get_selectors("mercari", "general", "title") or ["[data-testid='name']", "h1"]
    for selector in selectors:
        nodes = _safe_css(page, selector)
        if nodes:
            title = _node_text(nodes[0])
            if title:
                return title

    if isinstance(product_jsonld, dict):
        raw_name = product_jsonld.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            return raw_name.strip()

    meta_nodes = _safe_css(page, "meta[property='og:title']")
    if meta_nodes:
        return _node_attr(meta_nodes[0], "content")

    return ""


def _extract_description(page, body_text: str) -> str:
    selectors = get_selectors("mercari", "general", "description") or ["[data-testid='description']"]
    for selector in selectors:
        nodes = _safe_css(page, selector)
        if nodes:
            text = _node_text(nodes[0])
            if text:
                return text

    if not body_text or "商品の説明" not in body_text:
        return ""

    after = body_text.split("商品の説明", 1)[1]
    end_pos = len(after)
    for marker in ("商品の情報", "商品情報", "出品者", "コメント"):
        idx = after.find(marker)
        if idx != -1 and idx < end_pos:
            end_pos = idx
    return after[:end_pos].strip()


def _extract_image_urls(page, product_jsonld: Optional[dict]) -> tuple[list, str]:
    selectors = list(get_selectors("mercari", "general", "images") or [])
    for fallback_selector in _PRODUCT_IMAGE_FALLBACK:
        if fallback_selector not in selectors:
            selectors.append(fallback_selector)

    dom_image_urls = []
    for selector in selectors:
        for image_node in _safe_css(page, selector):
            image_url = _extract_node_image_url(image_node)
            if image_url and image_url not in dom_image_urls:
                dom_image_urls.append(image_url)

    jsonld_image_urls = _collect_jsonld_images(product_jsonld)
    html_image_urls = _extract_embedded_html_images(page)
    meta_image_urls = _extract_meta_image(page)

    merged_image_urls, image_source = _merge_image_sources(
        ("dom", dom_image_urls),
        ("jsonld", jsonld_image_urls),
        ("html", html_image_urls),
        ("meta", meta_image_urls),
    )
    return merged_image_urls, image_source


def _extract_variants(page, price: Optional[int]) -> list:
    variants = []
    selectors = [
        "mer-item-thumbnail ~ div button",
        "button[aria-haspopup='listbox']",
        "div[role='radiogroup'] div[role='radio']",
        "div[data-testid='product-variant-selector'] button",
    ]

    found_elements = []
    for selector in selectors:
        found_elements = _safe_css(page, selector)
        if len(found_elements) > 1:
            break

    seen_values = set()
    for element in found_elements:
        value = _node_text(element)
        if not value or value in seen_values:
            continue
        seen_values.add(value)
        variants.append(
            {
                "option1_value": value,
                "price": price,
                "inventory_qty": 1,
            }
        )
    return variants


def _dedupe_urls(values) -> list:
    seen = set()
    out = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def _find_network_payload_candidate(node):
    candidates = []

    def _score(candidate):
        score = 0
        title = candidate.get("name") or candidate.get("title")
        if isinstance(title, str) and title.strip():
            score += 4
        if _extract_payload_price(candidate) is not None:
            score += 3
        if _extract_payload_images(candidate):
            score += 2
        if isinstance(candidate.get("description"), str) and candidate.get("description").strip():
            score += 1
        return score

    def _walk(value):
        if isinstance(value, dict):
            candidates.append(value)
            for nested in value.values():
                _walk(nested)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(node)
    scored = sorted(((_score(candidate), candidate) for candidate in candidates), key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return {}


def _extract_payload_price(candidate: dict) -> Optional[int]:
    for key in ("price", "priceAmount", "amount", "itemPrice", "displayPrice", "value"):
        value = candidate.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            nested = _extract_payload_price(value)
            if nested is not None:
                return nested
        elif isinstance(value, (int, float)):
            return int(value)
        else:
            parsed = _extract_plain_number(str(value))
            if parsed is not None:
                return parsed

    for key in ("salePrice", "priceInfo"):
        value = candidate.get(key)
        if isinstance(value, dict):
            nested = _extract_payload_price(value)
            if nested is not None:
                return nested
    return None


def _extract_payload_images(candidate: dict) -> list:
    image_urls = []
    image_field_names = {"photos", "photo", "images", "image", "thumbnails", "imageurls", "image_urls"}
    url_field_names = {"url", "originalurl", "src", "imageurl", "image_url"}

    def _append_from_value(value, *, image_context: bool = False):
        if isinstance(value, str):
            if not image_context:
                return
            _append_unique_image_url(image_urls, value)
            return

        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                normalized_key = str(nested_key or "").strip().lower()
                if normalized_key in url_field_names:
                    _append_from_value(nested_value, image_context=True)
                    continue
                _append_from_value(
                    nested_value,
                    image_context=image_context or normalized_key in image_field_names,
                )
            return

        if isinstance(value, list):
            for item in value:
                _append_from_value(item, image_context=image_context)

    _append_from_value(candidate)

    return image_urls


def _extract_payload_status(candidate: dict) -> tuple[str, list[str]]:
    status_value = candidate.get("status") or candidate.get("itemStatus")
    if isinstance(status_value, str):
        normalized = status_value.strip().lower()
        if normalized in {"on_sale", "onsale", "active", "selling", "available"}:
            return "on_sale", [f"payload-status:{normalized}"]
        if normalized in {"sold_out", "soldout", "sold", "inactive"}:
            return "sold", [f"payload-status:{normalized}"]

    for key in ("soldOut", "isSoldOut"):
        value = candidate.get(key)
        if value is True:
            return "sold", [f"payload-flag:{key}"]
        if value is False:
            return "on_sale", [f"payload-flag:{key}"]

    return "unknown", []


def parse_mercari_network_payload(payload: dict, url: str) -> tuple[dict, dict]:
    item = _empty_item(url)
    candidate = _find_network_payload_candidate(payload)
    if not candidate:
        return item, {
            "strategy": "payload",
            "confidence": "low",
            "reasons": ["payload-candidate-missing"],
            "field_sources": {},
        }

    field_sources = {}
    title, title_source = run_detail_field_strategies(
        DetailFieldStrategy("payload", candidate.get("name") or candidate.get("title")),
        validator=_is_nonempty_text,
        default="",
    )
    if title:
        item["title"] = title.strip()
        field_sources["title"] = title_source

    price = _extract_payload_price(candidate)
    if _is_positive_price(price):
        item["price"] = price
        field_sources["price"] = "payload"

    description, description_source = run_detail_field_strategies(
        DetailFieldStrategy("payload", candidate.get("description")),
        validator=_is_nonempty_text,
        default="",
    )
    if description:
        item["description"] = str(description).strip()
        field_sources["description"] = description_source

    image_urls = _extract_payload_images(candidate)
    if payload is not candidate:
        for payload_image_url in _extract_payload_images(payload):
            _append_unique_image_url(image_urls, payload_image_url)
    if image_urls:
        item["image_urls"] = image_urls
        field_sources["image_urls"] = "payload"

    status, status_reasons = _extract_payload_status(candidate)
    item["status"] = status
    if status != "unknown":
        field_sources["status"] = "payload"

    attach_extraction_trace(item, strategy="payload", field_sources=field_sources)
    meta = {
        "strategy": "payload",
        "confidence": "high" if item["title"] and item["price"] is not None else "medium",
        "reasons": ["payload-candidate-found"] + status_reasons,
        "field_sources": field_sources,
    }
    return item, meta


def _extract_dom_price(page) -> Optional[int]:
    selectors = get_selectors("mercari", "general", "price") or ["[data-testid='price']"]
    for selector in selectors:
        nodes = _safe_css(page, selector)
        if not nodes:
            continue
        text = _node_text(nodes[0])
        price = _extract_price_from_text(text)
        if price is None:
            price = _extract_plain_number(text)
        if price is not None:
            return price
    return None


def _extract_scoped_text_price(page) -> Optional[int]:
    region_selectors = (
        "[data-testid='price'] *",
        "main span",
        "main div",
        "main p",
        "article span",
        "article div",
        "article p",
        "section span",
        "section div",
        "section p",
    )
    unique_prices = set()
    for selector in region_selectors:
        for node in _safe_css(page, selector):
            text = _node_text(node)
            if not text or len(text) > 80:
                continue
            if "¥" not in text and "￥" not in text and "円" not in text:
                continue
            price = _extract_price_from_text(text)
            if price is not None:
                unique_prices.add(price)
        if len(unique_prices) > 1:
            return None
    return next(iter(unique_prices)) if len(unique_prices) == 1 else None


def _extract_status(page, body_text: str, availability: str, deleted: bool) -> tuple[str, list[str], str]:
    """Aggregate multiple pieces of evidence before deciding status.

    Returns ``(status, reasons, evidence_strength)`` where
    *evidence_strength* is one of ``"hard"``, ``"soft"``, or ``"none"``.

    Evidence classification:
      hard_positive  – strong proof the item is on sale (jsonld InStock,
                       enabled purchase/checkout button)
      hard_negative  – strong proof the item is sold (jsonld OutOfStock,
                       *visible* sold-out badge with actual text)
      soft_negative  – weak sold signal that must NOT override a hard positive
                       (disabled checkout button alone, body-text "sold" markers)
    """
    hard_positives: list[str] = []
    hard_negatives: list[str] = []
    soft_negatives: list[str] = []
    reasons: list[str] = []

    # ── 0. Deleted pages are always authoritative ──────────────────────
    if deleted:
        reasons.append("deleted-marker")
        return "deleted", reasons, "hard"

    # ── 1. JSON-LD availability (structured data = high trust) ─────────
    if "outofstock" in availability or "soldout" in availability:
        hard_negatives.append("jsonld-out-of-stock")
    if "instock" in availability:
        hard_positives.append("jsonld-in-stock")

    # ── 2. Sold-out badges — only count if *visible* and has text ──────
    badges = get_selectors("mercari", "general", "status_badges") or _DEFAULT_STATUS_BADGE_SELECTORS
    for badge_sel in badges:
        for badge_node in _safe_css(page, badge_sel):
            aria_hidden = _node_attr(badge_node, "aria-hidden").lower()
            if aria_hidden == "true":
                reasons.append("sold-badge-hidden-skipped")
                continue
            badge_text = _node_text(badge_node)
            if badge_text and any(m in badge_text for m in ("SOLD", "売り切れ", "sold")):
                hard_negatives.append("sold-badge-visible")
            elif badge_text:
                # Badge present with unexpected text — treat as soft signal
                soft_negatives.append(f"sold-badge-text:{badge_text[:30]}")
            else:
                # Empty badge DOM node — could be hydration leftover
                soft_negatives.append("sold-badge-empty")

    # ── 3. Checkout / purchase buttons ────────────────────────────────
    button_selectors = get_selectors("mercari", "general", "checkout_button") or _DEFAULT_CHECKOUT_BUTTON_SELECTORS
    for sel in button_selectors:
        for button in _safe_css(page, sel):
            button_text = _node_text(button)
            disabled = _node_attr(button, "disabled")
            aria_disabled = _node_attr(button, "aria-disabled").lower()
            is_disabled = bool(disabled) or aria_disabled == "true" or "売り切れ" in button_text
            if is_disabled:
                soft_negatives.append("checkout-button-disabled")
            elif any(m in button_text for m in _ACTIVE_BUTTON_MARKERS) or "購入" in button_text:
                hard_positives.append("checkout-button-enabled")

    # Generic <button> elements
    for button in _safe_css(page, "button"):
        button_text = _node_text(button)
        if not button_text:
            continue
        if any(m in button_text for m in _ACTIVE_BUTTON_MARKERS):
            disabled = _node_attr(button, "disabled")
            aria_disabled = _node_attr(button, "aria-disabled").lower()
            if not disabled and aria_disabled != "true":
                hard_positives.append("purchase-button-enabled")
            else:
                soft_negatives.append("purchase-button-disabled")
        elif "売り切れ" in button_text:
            soft_negatives.append("sold-button-text")

    # ── 4. Body text markers (weakest signal) ─────────────────────────
    if any(m in body_text for m in _SOLD_MARKERS):
        soft_negatives.append("body-sold-marker")
    if any(m in body_text for m in _ACTIVE_BUTTON_MARKERS):
        hard_positives.append("body-purchase-marker")

    # ── Aggregate and decide ──────────────────────────────────────────
    reasons.extend(hard_positives)
    reasons.extend(hard_negatives)
    reasons.extend(soft_negatives)

    has_hard_positive = bool(hard_positives)
    has_hard_negative = bool(hard_negatives)
    has_soft_negative = bool(soft_negatives)

    # Hard positive beats soft negative — protect active items
    if has_hard_positive and not has_hard_negative:
        return "on_sale", reasons, "hard"

    # Hard negative with no hard positive → sold
    if has_hard_negative and not has_hard_positive:
        return "sold", reasons, "hard"

    # Both hard positive AND hard negative → trust positive (e.g. stale badge)
    if has_hard_positive and has_hard_negative:
        reasons.append("conflict-positive-wins")
        return "on_sale", reasons, "hard"

    # Only soft negatives, no hard evidence either way → do NOT confirm sold
    if has_soft_negative and not has_hard_positive:
        return "sold", reasons, "soft"

    return "unknown", reasons, "none"


def _classify_page(
    title: str,
    page_title: str,
    body_text: str,
    product_jsonld: Optional[dict],
    meta_price: Optional[int],
    dom_price: Optional[int],
    status: str,
) -> tuple[str, list[str]]:
    reasons = []
    missing_marker = next((marker for marker in _MISSING_PAGE_MARKERS if marker in body_text), "")
    if missing_marker:
        reasons.append(f"missing-marker:{missing_marker}")
        return "deleted_detail", reasons

    has_structured_product = isinstance(product_jsonld, dict)
    has_product_signal = bool(title or meta_price is not None or dom_price is not None or has_structured_product)

    if not has_product_signal and page_title in _HOME_TITLES:
        reasons.append("home-title-without-product-signals")
        return "deleted_detail", reasons

    if status == "sold":
        reasons.append("classified-sold")
        return "sold_detail", reasons
    if status == "on_sale":
        reasons.append("classified-active")
        return "active_detail", reasons
    if has_product_signal:
        reasons.append("product-signals-without-status")
        return "unknown_detail", reasons

    reasons.append("no-product-signals")
    return "unknown_page", reasons


def parse_mercari_item_page(page, url: str) -> tuple[dict, dict]:
    item = _empty_item(url)
    body_text = _extract_body_text(page)
    page_title = _extract_page_title(page)
    product_jsonld = _extract_product_jsonld(page)

    meta_price = _extract_meta_price(page)
    jsonld_price = _extract_jsonld_price(product_jsonld)
    dom_price = _extract_dom_price(page)
    title = _extract_title(page, product_jsonld)
    availability = _extract_jsonld_availability(product_jsonld)
    deleted = any(marker in body_text for marker in _MISSING_PAGE_MARKERS)

    status, status_reasons, evidence_strength = _extract_status(page, body_text, availability, deleted=deleted)
    page_type, page_reasons = _classify_page(
        title=title,
        page_title=page_title,
        body_text=body_text,
        product_jsonld=product_jsonld,
        meta_price=meta_price,
        dom_price=dom_price,
        status=status,
    )

    scoped_price = None
    if page_type in {"active_detail", "sold_detail"}:
        scoped_price = _extract_scoped_text_price(page)

    price, price_source = run_detail_field_strategies(
        DetailFieldStrategy("meta", meta_price),
        DetailFieldStrategy("jsonld", jsonld_price),
        DetailFieldStrategy("dom", dom_price),
        DetailFieldStrategy("scoped_text", scoped_price),
        validator=_is_positive_price,
        default=None,
    )
    price_source = price_source or "none"

    confidence = "low"
    if price_source in {"meta", "jsonld"}:
        confidence = "high"
    elif price_source in {"dom", "scoped_text"}:
        confidence = "medium"
    elif status in {"sold", "deleted"} and page_type in {"sold_detail", "deleted_detail"}:
        confidence = "high"
    elif page_type in {"active_detail", "sold_detail"}:
        confidence = "medium"

    if status == "on_sale" and price is None:
        confidence = "low"

    if page_type in {"deleted_detail", "unknown_page"}:
        price = None
        if status != "deleted":
            status = "deleted" if page_type == "deleted_detail" else "unknown"
        price_source = "none"

    description = ""
    if page_type not in {"deleted_detail", "unknown_page"} and status != "deleted":
        description = _extract_description(page, body_text)
    image_urls, image_source = _extract_image_urls(page, product_jsonld)
    variants = _extract_variants(page, price)

    item.update(
        {
            "title": title,
            "price": price,
            "status": status,
            "description": description,
            "image_urls": image_urls,
            "variants": variants,
        }
    )

    field_sources = {
        "title": "jsonld" if isinstance(product_jsonld, dict) and title and product_jsonld.get("name") == title else "dom" if title else "",
        "price": price_source,
        "status": "jsonld" if availability else "dom",
        "description": "dom" if description else "",
        "image_urls": image_source if image_urls else "",
        "variants": "dom" if variants else "",
    }
    strategy = "jsonld" if price_source == "jsonld" else "meta" if price_source == "meta" else "dom"
    attach_extraction_trace(item, strategy=strategy, field_sources=field_sources)

    meta = {
        "page_type": page_type,
        "price_source": price_source,
        "confidence": confidence,
        "evidence_strength": evidence_strength,
        "reasons": status_reasons + page_reasons,
        "strategy": strategy,
        "field_sources": {field: source for field, source in field_sources.items() if source},
    }
    return item, meta
