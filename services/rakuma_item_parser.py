"""
Shared Rakuma item-detail parsing helpers.

Both the full Rakuma scraper and the lightweight patrol use the same parsing
logic so status and price extraction do not drift over time.
"""
import json
import logging
import re

from selector_config import get_selectors

logger = logging.getLogger("rakuma.parser")

_MISSING_ITEM_MARKERS = (
    "お探しの商品は見つかりません",
    "ページが見つかりません",
    "商品が見つかりません",
    "この商品は削除されています",
    "すでに削除されています",
)

_SOLD_STATUS_SELECTORS = (
    "span.soldout",
    ".soldout-section",
    ".label-soldout",
    ".item-sell-out-badge",
    ".photo-box__soldout_ribbon",
    ".type-modal__contents--button--sold",
)

_ACTIVE_PURCHASE_SELECTORS = (
    "a.btn_buy",
    ".ga-item-buybutton",
    "a[href*='ref_action=btn_buy']",
)

_SOLD_TEXT_MARKERS = (
    "SOLD",
    "sold",
    "売り切れ",
    "販売終了",
)


def _clean_text(text) -> str:
    if not isinstance(text, str):
        return ""
    return text.replace("\xa0", " ").replace("\u3000", " ").strip()


def _node_text(node) -> str:
    if node is None:
        return ""

    text = _clean_text(getattr(node, "text", ""))
    if text:
        return text

    for method_name in ("get_text", "get_all_text"):
        extractor = getattr(node, method_name, None)
        if not callable(extractor):
            continue
        try:
            text = _clean_text(extractor() or "")
        except Exception as exc:
            logger.debug("Error getting Rakuma node text via %s: %s", method_name, exc)
            continue
        if text:
            return text

    return ""


def _node_attrib(node) -> dict:
    attrib = getattr(node, "attrib", {}) or {}
    if isinstance(attrib, dict):
        return attrib

    try:
        return dict(attrib.items())
    except Exception:
        try:
            return dict(attrib)
        except Exception:
            return {}


def first_node(page, selector: str):
    """Support both `page.css()` and older mocks exposing only `css_first()`."""
    try:
        nodes = page.css(selector)
        if nodes:
            return nodes[0]
    except Exception:
        pass

    try:
        return page.css_first(selector)
    except Exception:
        return None


def _has_any_selector(page, selectors) -> bool:
    for selector in selectors:
        if first_node(page, selector):
            return True
    return False


def _extract_price_value(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return int(raw_value)

    text = _clean_text(str(raw_value))
    if not text:
        return None

    match = re.search(r"[¥￥]\s*([\d,]+)", text) or re.search(r"([\d,]+)", text)
    if not match:
        return None

    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_rakuma_meta_price(page):
    selectors = [
        "meta[property='product:price:amount']",
        "meta[name='product:price:amount']",
    ]
    for selector in selectors:
        node = first_node(page, selector)
        if not node:
            continue
        attrib = _node_attrib(node)
        price = _extract_price_value(attrib.get("content"))
        if price is not None:
            return price
    return None


def _extract_rakuma_product_jsonld(page) -> dict:
    try:
        scripts = page.css("script[type='application/ld+json']")
    except Exception as exc:
        logger.debug("Error loading Rakuma JSON-LD scripts: %s", exc)
        return {}

    def _find_product(node):
        if isinstance(node, list):
            for item in node:
                found = _find_product(item)
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
                found = _find_product(node.get(key))
                if found:
                    return found

        return None

    for script in scripts:
        raw_text = _clean_text(getattr(script, "text", ""))
        if not raw_text:
            continue
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        product = _find_product(payload)
        if product:
            return product

    return {}


def _extract_jsonld_offer_price(offers):
    if isinstance(offers, dict):
        for key in ("price", "lowPrice", "highPrice"):
            price = _extract_price_value(offers.get(key))
            if price is not None:
                return price
        nested = offers.get("offers")
        if nested:
            return _extract_jsonld_offer_price(nested)

    if isinstance(offers, list):
        for offer in offers:
            price = _extract_jsonld_offer_price(offer)
            if price is not None:
                return price

    return None


def _extract_jsonld_availability(offers) -> str:
    if isinstance(offers, dict):
        availability = _clean_text(str(offers.get("availability") or ""))
        if availability:
            return availability.lower()
        nested = offers.get("offers")
        if nested:
            return _extract_jsonld_availability(nested)

    if isinstance(offers, list):
        for offer in offers:
            availability = _extract_jsonld_availability(offer)
            if availability:
                return availability

    return ""


def _normalize_page_title(page_title: str) -> str:
    title = _clean_text(page_title)
    if not title:
        return ""
    for suffix in (" | ラクマ", "の商品写真", " - ラクマ", " - フリマアプリ ラクマ"):
        if suffix in title:
            title = title.split(suffix)[0].strip()
    if ")の" in title:
        title = title.split(")の", 1)[-1].strip()
    return title


def _normalize_image_url(raw_url) -> str:
    if not isinstance(raw_url, str):
        return ""
    url = raw_url.strip().strip("'\"")
    url = url.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    if url.startswith("//"):
        url = "https:" + url
    return url


def _is_rakuma_product_image_url(raw_url) -> bool:
    url = _normalize_image_url(raw_url)
    return bool(re.match(r"^https?://img\.fril\.jp/img/[^/]+/[a-z]/[^/?#]+\.(?:jpe?g|png|webp)(?:[?#].*)?$", url, re.I))


def _rakuma_image_group_id(raw_url) -> str:
    url = _normalize_image_url(raw_url)
    match = re.match(r"^https?://img\.fril\.jp/img/([^/]+)/", url, re.I)
    return match.group(1) if match else ""


def _append_unique_image_url(image_urls: list, raw_url) -> None:
    url = _normalize_image_url(raw_url)
    if not _is_rakuma_product_image_url(url):
        return
    if url not in image_urls:
        image_urls.append(url)


def _collect_jsonld_images(product_jsonld: dict) -> list:
    raw_images = product_jsonld.get("image")
    image_urls = []
    if isinstance(raw_images, str):
        _append_unique_image_url(image_urls, raw_images)
    elif isinstance(raw_images, dict):
        for key in ("url", "contentUrl", "image", "src"):
            _append_unique_image_url(image_urls, raw_images.get(key))
    elif isinstance(raw_images, list):
        for image in raw_images:
            if isinstance(image, str):
                _append_unique_image_url(image_urls, image)
            elif isinstance(image, dict):
                for key in ("url", "contentUrl", "image", "src"):
                    before_count = len(image_urls)
                    _append_unique_image_url(image_urls, image.get(key))
                    if len(image_urls) > before_count:
                        break
    return image_urls


def _extract_node_image_url(node) -> str:
    attrib = _node_attrib(node)

    for attr_name in ("src", "data-lazy", "data-src", "data-original", "data-image", "data-image-url"):
        candidate = attrib.get(attr_name)
        if candidate and isinstance(candidate, str) and "placeholder" not in candidate.lower() and "blank" not in candidate.lower():
            return _normalize_image_url(candidate)

    for attr_name in ("srcset", "data-srcset"):
        srcset = attrib.get(attr_name)
        if not isinstance(srcset, str):
            continue
        for part in srcset.split(","):
            candidate = part.strip().split(" ")[0]
            if candidate and "placeholder" not in candidate.lower() and "blank" not in candidate.lower():
                return _normalize_image_url(candidate)

    style = attrib.get("style", "")
    if isinstance(style, str):
        match = re.search(r"background-image:\s*url\(([^)]+)\)", style)
        if match:
            return _normalize_image_url(match.group(1))

    return ""


def _select_dominant_image_group_id(image_urls: list) -> str:
    counts = {}
    order = []
    for image_url in image_urls:
        group_id = _rakuma_image_group_id(image_url)
        if not group_id:
            continue
        if group_id not in counts:
            counts[group_id] = 0
            order.append(group_id)
        counts[group_id] += 1

    if not counts:
        return ""
    return max(order, key=lambda group_id: counts[group_id])


def _filter_images_to_group(image_urls: list, group_id: str) -> list:
    if not group_id:
        return image_urls
    return [image_url for image_url in image_urls if _rakuma_image_group_id(image_url) == group_id]


def _extract_dom_image_urls(page, product_jsonld: dict) -> list:
    image_selectors = get_selectors("rakuma", "detail", "images") or [".sp-image"]
    jsonld_images = _collect_jsonld_images(product_jsonld)
    active_group_id = _select_dominant_image_group_id(jsonld_images)

    image_urls = []
    for selector in image_selectors:
        try:
            imgs = page.css(selector)
        except Exception as exc:
            logger.debug("Error extracting Rakuma images for %s: %s", selector, exc)
            continue

        selector_urls = []
        for img in imgs:
            _append_unique_image_url(selector_urls, _extract_node_image_url(img))

        if not selector_urls:
            continue

        if active_group_id:
            selector_urls = _filter_images_to_group(selector_urls, active_group_id)
        else:
            active_group_id = _select_dominant_image_group_id(selector_urls)
            selector_urls = _filter_images_to_group(selector_urls, active_group_id)

        for image_url in selector_urls:
            _append_unique_image_url(image_urls, image_url)

    return image_urls


def extract_rakuma_page_text(page) -> str:
    """Return the most useful page-wide text available from a Scrapling page."""
    for attr_name in ("get_text", "get_all_text"):
        extractor = getattr(page, attr_name, None)
        if not callable(extractor):
            continue
        try:
            text = extractor() or ""
        except Exception as exc:
            logger.debug("Error getting Rakuma page text via %s: %s", attr_name, exc)
            continue
        text = _clean_text(text)
        if text:
            return text

    try:
        body_nodes = page.css("body")
        if body_nodes:
            return _node_text(body_nodes[0])
    except Exception as exc:
        logger.debug("Error getting Rakuma body text via css('body'): %s", exc)

    return ""


def is_rakuma_missing_item_page(body_text: str) -> bool:
    """Detect strong Rakuma not-found / removed-item markers."""
    if not body_text:
        return False
    return any(marker in body_text for marker in _MISSING_ITEM_MARKERS)


def parse_rakuma_item_page(page, url: str, body_text: str | None = None) -> dict:
    """
    Extract Rakuma item fields from a Scrapling page object.

    The return shape matches the existing `rakuma_db.scrape_item_detail()` output.
    """
    if body_text is None:
        body_text = extract_rakuma_page_text(page)

    if body_text:
        logger.debug("Rakuma page text preview: %s", body_text[:500])

    if is_rakuma_missing_item_page(body_text):
        return {
            "url": url,
            "title": "",
            "price": None,
            "status": "deleted",
            "description": "",
            "image_urls": [],
            "variants": [],
        }

    product_jsonld = _extract_rakuma_product_jsonld(page)
    page_title = _node_text(first_node(page, "title"))
    if page_title:
        logger.debug("Rakuma <title>: %s", page_title)

    try:
        for tag in ("h1", "h2", "h3", "h4"):
            for el in page.css(tag):
                text = _node_text(el)
                if text:
                    logger.debug("Rakuma <%s>: %s", tag, text[:100])
    except Exception as exc:
        logger.debug("Error inspecting Rakuma heading tags: %s", exc)

    title = ""
    title_selectors = get_selectors("rakuma", "detail", "title") or ["h1.item__name", "h1"]
    for selector in title_selectors:
        el = first_node(page, selector)
        title = _node_text(el)
        if title:
            break

    if not title:
        title = _clean_text(str(product_jsonld.get("name") or ""))

    if not title and page_title:
        title = _normalize_page_title(page_title)
        logger.debug("Rakuma title recovered from <title>: %s", title)

    price = _extract_rakuma_meta_price(page)
    if price is None:
        price = _extract_jsonld_offer_price(product_jsonld.get("offers"))

    price_selectors = get_selectors("rakuma", "detail", "price") or [
        "span.item__price",
        ".item__price",
        "p.item__price",
        ".item-box__item-price",
        "[data-testid='price']",
    ]
    for selector in price_selectors:
        if price is not None:
            break
        el = first_node(page, selector)
        price_text = _node_text(el)
        if not price_text:
            continue
        price = _extract_price_value(price_text)

    description = ""
    desc_selectors = get_selectors("rakuma", "detail", "description") or ["div.item__description", ".item-description"]
    for selector in desc_selectors:
        el = first_node(page, selector)
        description = _node_text(el)
        if description:
            break

    if not description:
        description = _clean_text(str(product_jsonld.get("description") or ""))

    if not description and body_text:
        idx = body_text.find("商品説明")
        if idx >= 0:
            end_idx = body_text.find("商品情報", idx)
            if end_idx < 0:
                end_idx = idx + 500
            description = body_text[idx + len("商品説明"):end_idx].strip()

    image_urls = _extract_dom_image_urls(page, product_jsonld)
    if not image_urls:
        image_urls = _collect_jsonld_images(product_jsonld)

    availability = _extract_jsonld_availability(product_jsonld.get("offers"))
    status = "unknown"
    if "outofstock" in availability or "soldout" in availability:
        status = "sold"
    elif "instock" in availability:
        status = "on_sale"

    if _has_any_selector(page, _SOLD_STATUS_SELECTORS):
        status = "sold"
    elif status != "sold" and _has_any_selector(page, _ACTIVE_PURCHASE_SELECTORS):
        status = "on_sale"

    if status == "unknown" and body_text:
        if any(m in body_text for m in _SOLD_TEXT_MARKERS):
            status = "sold"

    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": [],
    }
