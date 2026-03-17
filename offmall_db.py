"""
Offmall (Hard Off) scraping module.
Uses Scrapling HTTP fetches for product detail pages and search results.
"""
import json
import logging
import re
from urllib.parse import urljoin

logger = logging.getLogger("offmall")


SELECTORS = {
    "product_links": [
        "a[href*='/product/']",
        "a.product-card__link",
        "a[class*='product']",
    ],
    "price": [
        "span.product-detail-price__main",
        ".product-detail-price__main",
    ],
}


def _empty_result(url: str, status: str = "error") -> dict:
    return {
        "url": url,
        "title": "",
        "price": None,
        "status": status,
        "description": "",
        "image_urls": [],
        "variants": [],
        "brand": "",
        "condition": "",
    }


def _resolve_detail_url(url_or_driver, maybe_url=None) -> str:
    if isinstance(maybe_url, str) and maybe_url:
        return maybe_url
    if isinstance(url_or_driver, str) and url_or_driver:
        return url_or_driver
    raise ValueError("url is required")


def _get_page_text(page) -> str:
    for attr_name in ("get_all_text", "get_text"):
        extractor = getattr(page, attr_name, None)
        if not callable(extractor):
            continue
        try:
            text = extractor() or ""
        except Exception:
            continue
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _extract_price_digits(text: str):
    if not text:
        return None
    match = re.search(r"([0-9][0-9,]{1,})", text.replace("，", ","))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_json_ld_product(page) -> dict:
    scripts = page.css("script[type='application/ld+json']")
    for script_el in scripts:
        try:
            raw = str(script_el.text or "").strip()
            if not raw:
                continue
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        return item
        except (json.JSONDecodeError, Exception):
            continue
    return {}


def _extract_visible_price(page, page_text: str = ""):
    for selector in SELECTORS["price"]:
        for el in page.css(selector):
            price = _extract_price_digits(str(el.text or "").strip())
            if price is not None:
                return price

    if page_text:
        match = re.search(r"([0-9][0-9,]{1,})\s*(?:円)?\s*\(税込\)", page_text)
        if match:
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:
                return None

    return None


def _infer_offmall_status(page_text: str, offers=None) -> str:
    availability = ""
    if isinstance(offers, dict):
        availability = str(offers.get("availability", "") or "")

    if "InStock" in availability:
        return "active"
    if "OutOfStock" in availability:
        return "sold"

    if "対象の商品はございません" in page_text or "ページが見つかりません" in page_text:
        return "sold"
    if "カートに入れる" in page_text or "購入手続き" in page_text:
        return "active"

    return "unknown"


def scrape_item_detail_light(url: str) -> dict:
    """HTTP-only Offmall detail scrape via JSON-LD parsing."""
    try:
        from services.scraping_client import fetch_static

        page = fetch_static(url)
        page_text = _get_page_text(page)
        if "対象の商品はございません" in page_text or "ページが見つかりません" in page_text:
            return {
                "url": url,
                "title": "Sold/Removed",
                "price": None,
                "status": "sold",
                "description": "",
                "image_urls": [],
                "variants": [],
                "brand": "",
                "condition": "",
            }

        result = _empty_result(url, status="unknown")
        json_ld = _extract_json_ld_product(page)
        if not json_ld:
            return {}

        result["title"] = json_ld.get("name", "")
        brand = json_ld.get("brand", {})
        result["brand"] = brand.get("name", "") if isinstance(brand, dict) else str(brand or "")
        result["description"] = json_ld.get("description", "")

        # JSON-LD description is often just the URL on Offmall; fall back to HTML
        if not result["description"] or result["description"].startswith("http"):
            desc_parts = []
            # Staff recommendation section
            point_els = page.css("div.product-detail-point__box")
            if point_els:
                text = str(point_els[0].text or "").strip()
                if text:
                    desc_parts.append(text)
            # Features/notes table in #panel1
            for th_el in page.css("#panel1 th, .product-detail-spec th"):
                th_text = str(th_el.text or "")
                if "特徴" in th_text or "備考" in th_text or "商品説明" in th_text:
                    # Navigate to sibling td
                    parent = th_el.parent
                    if parent:
                        td_els = parent.css("td")
                        if td_els:
                            td_text = str(td_els[0].text or "").strip()
                            if td_text:
                                desc_parts.append(td_text)
            if desc_parts:
                result["description"] = "\n".join(desc_parts)

        offers = json_ld.get("offers", {})
        result["price"] = _extract_visible_price(page, page_text)
        if result["price"] is None and isinstance(offers, dict):
            price_str = str(offers.get("price", ""))
            if price_str:
                try:
                    result["price"] = int(float(price_str))
                except (ValueError, TypeError):
                    pass
        result["status"] = _infer_offmall_status(page_text, offers)

        images = json_ld.get("image", [])
        if isinstance(images, str):
            result["image_urls"] = [images]
        elif isinstance(images, list):
            result["image_urls"] = [img for img in images if isinstance(img, str)]

        og_el = page.css("meta[property='og:image']")
        if og_el:
            og_url = str(og_el[0].attrib.get("content", "") or "")
            if og_url.startswith("http") and og_url not in result["image_urls"]:
                result["image_urls"].insert(0, og_url)

        for img_el in page.css("img[src*='hardoff']"):
            src = str(img_el.attrib.get("src", "") or "")
            if src.startswith("http") and src not in result["image_urls"]:
                result["image_urls"].append(src)

        condition = json_ld.get("itemCondition", "")
        if condition:
            result["condition"] = re.sub(r"https?://schema\.org/", "", condition)
        else:
            cond_els = page.css(".item-condition, .condition, [class*='rank'], [class*='condition']")
            if cond_els:
                result["condition"] = str(cond_els[0].text or "").strip()

        if result["price"]:
            result["variants"] = [
                {
                    "option1_value": result.get("condition") or "Default Title",
                    "price": result["price"],
                    "sku": "",
                    "inventory_qty": 1 if result["status"] == "active" else 0,
                }
            ]

        return result
    except Exception as exc:
        logger.debug("Offmall light scrape error: %s", exc)
        return {}


def scrape_item_detail(url_or_driver, maybe_url=None, **_kwargs) -> dict:
    """
    Offmall detail scrape.
    The legacy `(driver, url)` signature is accepted for backward compatibility.
    """
    url = _resolve_detail_url(url_or_driver, maybe_url)
    return scrape_item_detail_light(url) or _empty_result(url)


def scrape_single_item(url: str, headless: bool = True) -> list:
    """Scrape a single Offmall product and return `list[dict]`."""
    result = scrape_item_detail(url)
    return [result] if result.get("title") else []


def _extract_search_urls(page, base_url: str, max_items: int) -> list:
    urls = []
    seen = set()
    for selector in SELECTORS["product_links"]:
        for anchor in page.css(selector):
            href = str(anchor.attrib.get("href", "") or "").strip()
            if not href:
                continue
            full_url = urljoin(base_url, href)
            if "netmall.hardoff.co.jp" not in full_url or "/product/" not in full_url:
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            urls.append(full_url)
            if len(urls) >= max_items:
                return urls
    return urls


def _find_next_page_url(page, current_url: str) -> str:
    for anchor in page.css("a[href]"):
        href = str(anchor.attrib.get("href", "") or "").strip()
        if not href:
            continue
        text = str(anchor.text or "").strip()
        classes = str(anchor.attrib.get("class", "") or "")
        if "次へ" in text or "next" in classes.lower():
            return urljoin(current_url, href)
    return ""


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
) -> list:
    """Scrape Offmall search results using HTTP-only page fetches."""
    results = []
    candidate_urls = []
    candidate_target = max(max_items, max_items * 2)

    try:
        from services.scraping_client import fetch_static

        current_url = search_url
        seen_pages = set()
        max_pages = max(1, max_scroll)

        while current_url and current_url not in seen_pages and len(seen_pages) < max_pages:
            seen_pages.add(current_url)
            page = fetch_static(current_url)
            for item_url in _extract_search_urls(page, current_url, max_items=candidate_target):
                if item_url not in candidate_urls:
                    candidate_urls.append(item_url)
                if len(candidate_urls) >= candidate_target:
                    break
            if len(candidate_urls) >= candidate_target:
                break
            current_url = _find_next_page_url(page, current_url)

        for item_url in candidate_urls:
            if len(results) >= max_items:
                break
            result = scrape_item_detail(item_url)
            if result.get("title"):
                results.append(result)

        return results
    except Exception as exc:
        logger.error("Error in scrape_search_result: %s", exc)
        return results
