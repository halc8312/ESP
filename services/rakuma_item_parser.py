"""
Shared Rakuma item-detail parsing helpers.

Both the full Rakuma scraper and the lightweight patrol use the same parsing
logic so status and price extraction do not drift over time.
"""
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


def _node_text(node) -> str:
    text = getattr(node, "text", "")
    return text.strip() if isinstance(text, str) else ""


def _node_attrib(node) -> dict:
    attrib = getattr(node, "attrib", {})
    return attrib if isinstance(attrib, dict) else {}


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
        if isinstance(text, str) and text.strip():
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

    if not title and page_title:
        title = page_title
        for suffix in (" | ラクマ", "の商品写真", " - ラクマ", " - フリマアプリ ラクマ"):
            if suffix in title:
                title = title.split(suffix)[0].strip()
        if ")の" in title:
            title = title.split(")の", 1)[-1].strip()
        logger.debug("Rakuma title recovered from <title>: %s", title)

    price = None
    price_selectors = get_selectors("rakuma", "detail", "price") or ["span.item__price", ".item__price"]
    for selector in price_selectors:
        el = first_node(page, selector)
        price_text = _node_text(el)
        if not price_text:
            continue
        match = re.search(r"[¥￥]\s*([\d,]+)", price_text) or re.search(r"([\d,]+)", price_text)
        if match:
            price = int(match.group(1).replace(",", ""))
            break

    if price is None and body_text:
        match = re.search(r"[¥￥]\s*([\d,]+)", body_text)
        if not match:
            match = re.search(r"([\d,]+)\s*円", body_text)
        if match:
            price = int(match.group(1).replace(",", ""))

    description = ""
    desc_selectors = get_selectors("rakuma", "detail", "description") or ["div.item__description", ".item-description"]
    for selector in desc_selectors:
        el = first_node(page, selector)
        description = _node_text(el)
        if description:
            break

    if not description and body_text:
        idx = body_text.find("商品説明")
        if idx >= 0:
            end_idx = body_text.find("商品情報", idx)
            if end_idx < 0:
                end_idx = idx + 500
            description = body_text[idx + len("商品説明"):end_idx].strip()

    image_urls = []
    image_selectors = get_selectors("rakuma", "detail", "images") or [".sp-image"]
    for selector in image_selectors:
        try:
            imgs = page.css(selector)
        except Exception as exc:
            logger.debug("Error extracting Rakuma images for %s: %s", selector, exc)
            continue
        for img in imgs:
            attrib = _node_attrib(img)
            src = attrib.get("src", "")
            src = src if isinstance(src, str) else ""
            if not src or "placeholder" in src.lower() or "blank" in src.lower():
                lazy_src = attrib.get("data-lazy") or attrib.get("data-src") or ""
                src = lazy_src if isinstance(lazy_src, str) else ""

            if not src:
                style = attrib.get("style", "")
                if isinstance(style, str):
                    match = re.search(r"background-image:\s*url\(([^)]+)\)", style)
                    if match:
                        src = match.group(1).strip("'\"")

            if src and src.startswith("http") and src not in image_urls:
                image_urls.append(src)

    status = "on_sale"
    sold_markers = ("SOLDOUT", "SOLD OUT", "売り切れ")
    if any(marker in body_text for marker in sold_markers):
        status = "sold"

    sold_selectors = ["span.soldout", ".soldout-section", ".label-soldout", ".item-sell-out-badge"]
    for selector in sold_selectors:
        sold_el = first_node(page, selector)
        if sold_el:
            status = "sold"
            break

    return {
        "url": url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": [],
    }
