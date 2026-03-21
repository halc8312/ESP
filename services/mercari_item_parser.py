"""
Shared Mercari item-detail parsing helpers.

The parser classifies a page before extracting price so generic shell content
cannot be mistaken for product price data.
"""
import json
import logging
import re
from typing import Optional

from services.extraction_policy import attach_extraction_trace, pick_first_valid
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
_PRODUCT_IMAGE_FALLBACK = [
    "img[src*='static.mercdn.net'][src*='/item/'][src*='/photos/']",
    "img[data-src*='static.mercdn.net'][data-src*='/item/'][data-src*='/photos/']",
    "img[data-lazy*='static.mercdn.net'][data-lazy*='/item/'][data-lazy*='/photos/']",
    "img[data-lazy-src*='static.mercdn.net'][data-lazy-src*='/item/'][data-lazy-src*='/photos/']",
    "img[srcset*='static.mercdn.net'][srcset*='/item/'][srcset*='/photos/']",
    "source[srcset*='static.mercdn.net'][srcset*='/item/'][srcset*='/photos/']",
]
_PLACEHOLDER_IMAGE_MARKERS = ("data:image", "placeholder", "blank", "transparent", "pixel")


def _is_nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_price(value) -> bool:
    return isinstance(value, int) and value > 0


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
    body_parts = []
    for node in _safe_css(page, "body *"):
        text = _node_text(node)
        if text:
            body_parts.append(text)
    if body_parts:
        return " ".join(body_parts)

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


def _extract_title(page, product_jsonld: Optional[dict]) -> str:
    title_nodes = _safe_css(page, "h1")
    if title_nodes:
        title = _node_text(title_nodes[0])
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


def _extract_description(body_text: str) -> str:
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

    image_urls = []
    for selector in selectors:
        for image_node in _safe_css(page, selector):
            image_url = _extract_node_image_url(image_node)
            if image_url and image_url not in image_urls:
                image_urls.append(image_url)
    if image_urls:
        return image_urls, "dom"

    jsonld_images = _collect_jsonld_images(product_jsonld)
    if jsonld_images:
        return jsonld_images, "jsonld"

    meta_images = _extract_meta_image(page)
    if meta_images:
        return meta_images, "meta"

    return [], ""


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
    for key in ("photos", "images", "image", "thumbnails"):
        raw = candidate.get(key)
        if isinstance(raw, str):
            image_urls.append(raw)
        elif isinstance(raw, dict):
            for nested_key in ("url", "originalUrl", "src", "imageUrl"):
                if raw.get(nested_key):
                    image_urls.append(str(raw[nested_key]))
                    break
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    image_urls.append(item)
                elif isinstance(item, dict):
                    for nested_key in ("url", "originalUrl", "src", "imageUrl"):
                        if item.get(nested_key):
                            image_urls.append(str(item[nested_key]))
                            break
    return [url for url in _dedupe_urls(image_urls) if url.startswith("http")]


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
    title, title_source = pick_first_valid(
        ("payload", candidate.get("name") or candidate.get("title")),
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

    description, description_source = pick_first_valid(
        ("payload", candidate.get("description")),
        validator=_is_nonempty_text,
        default="",
    )
    if description:
        item["description"] = str(description).strip()
        field_sources["description"] = description_source

    image_urls = _extract_payload_images(candidate)
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


def _extract_status(page, body_text: str, availability: str, deleted: bool) -> tuple[str, list[str]]:
    reasons = []
    if deleted:
        reasons.append("deleted-marker")
        return "deleted", reasons

    if "outofstock" in availability or "soldout" in availability:
        reasons.append("jsonld-out-of-stock")
        return "sold", reasons
    if "instock" in availability:
        reasons.append("jsonld-in-stock")
        return "on_sale", reasons

    for button in _safe_css(page, "button"):
        button_text = _node_text(button)
        if not button_text:
            continue
        if any(marker in button_text for marker in _ACTIVE_BUTTON_MARKERS):
            disabled = _node_attr(button, "disabled")
            aria_disabled = _node_attr(button, "aria-disabled").lower()
            if not disabled and aria_disabled != "true":
                reasons.append("purchase-button-enabled")
                return "on_sale", reasons
            reasons.append("purchase-button-disabled")
            return "sold", reasons
        if "売り切れ" in button_text:
            reasons.append("sold-button")
            return "sold", reasons

    if any(marker in body_text for marker in _SOLD_MARKERS):
        reasons.append("sold-marker")
        return "sold", reasons
    if any(marker in body_text for marker in _ACTIVE_BUTTON_MARKERS):
        reasons.append("purchase-marker")
        return "on_sale", reasons

    return "unknown", reasons


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

    status, status_reasons = _extract_status(page, body_text, availability, deleted=deleted)
    page_type, page_reasons = _classify_page(
        title=title,
        page_title=page_title,
        body_text=body_text,
        product_jsonld=product_jsonld,
        meta_price=meta_price,
        dom_price=dom_price,
        status=status,
    )

    price = None
    price_source = "none"
    if meta_price is not None:
        price = meta_price
        price_source = "meta"
    elif jsonld_price is not None:
        price = jsonld_price
        price_source = "jsonld"
    elif dom_price is not None:
        price = dom_price
        price_source = "dom"
    elif page_type in {"active_detail", "sold_detail"}:
        scoped_price = _extract_scoped_text_price(page)
        if scoped_price is not None:
            price = scoped_price
            price_source = "scoped_text"

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

    description = _extract_description(body_text)
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
        "reasons": status_reasons + page_reasons,
        "strategy": strategy,
        "field_sources": {field: source for field, source in field_sources.items() if source},
    }
    return item, meta
