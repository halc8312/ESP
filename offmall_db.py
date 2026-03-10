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


def scrape_item_detail_light(url: str) -> dict:
    """HTTP-only Offmall detail scrape via JSON-LD parsing."""
    try:
        from services.scraping_client import fetch_static

        page = fetch_static(url)
        page_text = str(page.get_all_text())
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
        scripts = page.css("script[type='application/ld+json']")
        json_ld = {}
        for script_el in scripts:
            try:
                raw = str(script_el.text or "").strip()
                if not raw:
                    continue
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    json_ld = data
                    break
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            json_ld = item
                            break
                if json_ld:
                    break
            except (json.JSONDecodeError, Exception):
                continue

        if not json_ld:
            return {}

        result["title"] = json_ld.get("name", "")
        brand = json_ld.get("brand", {})
        result["brand"] = brand.get("name", "") if isinstance(brand, dict) else str(brand or "")
        result["description"] = json_ld.get("description", "")

        offers = json_ld.get("offers", {})
        if isinstance(offers, dict):
            price_str = str(offers.get("price", ""))
            if price_str:
                try:
                    result["price"] = int(float(price_str))
                except (ValueError, TypeError):
                    pass
            availability = offers.get("availability", "")
            if "InStock" in availability:
                result["status"] = "active"
            elif "OutOfStock" in availability:
                result["status"] = "sold"

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

    try:
        from services.scraping_client import fetch_static

        current_url = search_url
        seen_pages = set()
        max_pages = max(1, max_scroll)

        while current_url and current_url not in seen_pages and len(seen_pages) < max_pages:
            seen_pages.add(current_url)
            page = fetch_static(current_url)
            for item_url in _extract_search_urls(page, current_url, max_items=max_items * 2):
                if item_url not in candidate_urls:
                    candidate_urls.append(item_url)
                if len(candidate_urls) >= max_items:
                    break
            if len(candidate_urls) >= max_items:
                break
            current_url = _find_next_page_url(page, current_url)

        for item_url in candidate_urls[:max_items]:
            result = scrape_item_detail(item_url)
            if result.get("title"):
                results.append(result)

        return results
    except Exception as exc:
        logger.error("Error in scrape_search_result: %s", exc)
        return results
