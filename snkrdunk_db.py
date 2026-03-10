"""
SNKRDUNK scraping module.
Uses Scrapling HTTP fetches for detail pages and Scrapling dynamic fetches for search pages.
"""
import json
import logging
import re
from urllib.parse import urljoin

from selector_config import get_selectors, get_valid_domains
from scrape_metrics import check_scrape_health, get_metrics, log_scrape_result

logger = logging.getLogger("snkrdunk")


def _empty_result(url: str, status: str = "error") -> dict:
    return {
        "url": url,
        "title": "",
        "price": None,
        "status": status,
        "description": "",
        "image_urls": [],
        "variants": [],
    }


def _resolve_detail_url(url_or_driver, maybe_url=None) -> str:
    if isinstance(maybe_url, str) and maybe_url:
        return maybe_url
    if isinstance(url_or_driver, str) and url_or_driver:
        return url_or_driver
    raise ValueError("url is required")


def _get_first_meta_content(page, selectors) -> str:
    for selector in selectors:
        els = page.css(selector)
        if not els:
            continue
        content = str(els[0].attrib.get("content", "") or "").strip()
        if content:
            return content
    return ""


def _get_first_text(page, selectors) -> str:
    for selector in selectors:
        els = page.css(selector)
        if not els:
            continue
        text = str(els[0].text or "").strip()
        if text:
            return text
    return ""


def _extract_price_value(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return int(raw_value)

    digits = "".join(ch for ch in str(raw_value) if ch.isdigit())
    if digits:
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def _collect_image_urls(raw_value) -> list:
    image_urls = []

    def _append(url):
        if url and isinstance(url, str) and url.startswith("http") and url not in image_urls:
            image_urls.append(url)

    if isinstance(raw_value, str):
        _append(raw_value)
    elif isinstance(raw_value, list):
        for item in raw_value:
            if isinstance(item, dict):
                _append(item.get("url") or item.get("src") or item.get("imageUrl"))
            else:
                _append(item)
    elif isinstance(raw_value, dict):
        _append(raw_value.get("url") or raw_value.get("src") or raw_value.get("imageUrl"))

    return image_urls


def _extract_product_jsonld(page):
    scripts = page.css("script[type='application/ld+json']")

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
                types = [str(t).lower() for t in node_type]
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
        raw_text = str(script.text or "").strip()
        if not raw_text:
            continue
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        product = _find_product(payload)
        if product:
            return product

    return None


def _extract_jsonld_availability(offers) -> str:
    if isinstance(offers, dict):
        availability = offers.get("availability")
        if isinstance(availability, str) and availability:
            return availability
        nested = offers.get("offers")
        if nested:
            return _extract_jsonld_availability(nested)
    elif isinstance(offers, list):
        for offer in offers:
            availability = _extract_jsonld_availability(offer)
            if availability:
                return availability
    return ""


def _extract_jsonld_price(offers):
    if isinstance(offers, dict):
        for key in ("lowPrice", "price", "highPrice"):
            price = _extract_price_value(offers.get(key))
            if price is not None:
                return price
        nested = offers.get("offers")
        if nested:
            return _extract_jsonld_price(nested)
    elif isinstance(offers, list):
        for offer in offers:
            price = _extract_jsonld_price(offer)
            if price is not None:
                return price
    return None


def _normalize_snkrdunk_title(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"の新品/中古.*$", "", normalized).strip()
    normalized = re.sub(r"\s*[|｜]\s*スニダン\s*$", "", normalized).strip()
    return normalized


def _parse_detail_page(page, url: str) -> dict:
    result = _empty_result(url, status="on_sale")
    product_jsonld = _extract_product_jsonld(page)

    if product_jsonld:
        result["title"] = (
            str(product_jsonld.get("name") or "").strip()
            or _normalize_snkrdunk_title(
                _get_first_meta_content(page, ["meta[property='og:title']", "meta[name='twitter:title']"])
                or _get_first_text(page, ["title"])
            )
        )

        price = _extract_jsonld_price(product_jsonld.get("offers"))
        if price is not None:
            result["price"] = price

        description = str(product_jsonld.get("description") or "").strip()
        if not description:
            description = _get_first_meta_content(page, ["meta[name='description']", "meta[property='og:description']"])
        result["description"] = description

        image_urls = _collect_image_urls(product_jsonld.get("image"))
        if not image_urls:
            image_urls = _collect_image_urls(_get_first_meta_content(page, ["meta[property='og:image']"]))
        result["image_urls"] = image_urls

        availability = _extract_jsonld_availability(product_jsonld.get("offers"))
        if availability:
            availability_lower = availability.lower()
            if "outofstock" in availability_lower:
                result["status"] = "sold"
            elif "instock" in availability_lower:
                result["status"] = "on_sale"

        if result.get("title"):
            return result

    script_el = page.find("#__NEXT_DATA__")

    if not script_el:
        logger.debug("No JSON item data found, falling back to CSS selectors")

        title_selectors = get_selectors("snkrdunk", "detail", "title") or ["h1.product-name-en", "p.product-name-jp", "h1"]
        result["title"] = _get_first_text(page, title_selectors)
        if not result["title"]:
            result["title"] = _normalize_snkrdunk_title(
                _get_first_meta_content(page, ["meta[property='og:title']", "meta[name='twitter:title']"])
                or _get_first_text(page, ["title"])
            )

        if not result.get("title"):
            return {}

        price_selectors = get_selectors("snkrdunk", "detail", "price") or ["span.product-lowest-price"]
        for sel in price_selectors:
            els = page.css(sel)
            if els and els[0].text:
                result["price"] = _extract_price_value(str(els[0].text).strip())
                if result["price"] is not None:
                    break

        if result["price"] is None:
            result["price"] = _extract_price_value(page.get_all_text() or "")

        desc_selectors = get_selectors("snkrdunk", "detail", "description") or ["div.product-acd-content.product-content-info-detail"]
        result["description"] = _get_first_text(page, desc_selectors)
        if not result["description"]:
            result["description"] = _get_first_meta_content(page, ["meta[name='description']", "meta[property='og:description']"])

        image_selectors = get_selectors("snkrdunk", "detail", "images") or [".product-img img"]
        image_urls = []
        for sel in image_selectors:
            els = page.css(sel)
            for el in els:
                src = el.attrib.get("src") or el.attrib.get("data-src")
                if src and src.startswith("http") and src not in image_urls:
                    image_urls.append(str(src))
            if image_urls:
                break
        if not image_urls:
            image_urls = _collect_image_urls(_get_first_meta_content(page, ["meta[property='og:image']"]))
        result["image_urls"] = image_urls

        page_text = str(page.get_all_text() or "")
        if "SOLD OUT" in page_text or "売り切れ" in page_text or "在庫なし" in page_text:
            result["status"] = "sold"

        return result

    json_str = str(script_el.text or "").strip()
    if not json_str:
        return {}

    data = json.loads(json_str)
    props = data.get("props", {})
    page_props = props.get("pageProps", {})
    item = (
        page_props.get("item")
        or page_props.get("product")
        or page_props.get("initialState", {}).get("item", {})
        or page_props.get("initialState", {}).get("product", {})
        or {}
    )
    if not item:
        return {}
    result["title"] = item.get("name") or item.get("title") or item.get("productName", "")

    price_raw = item.get("price") or item.get("lowestPrice") or item.get("minPrice")
    if price_raw is not None:
        try:
            result["price"] = int(price_raw)
        except (ValueError, TypeError):
            pass

    description = item.get("description") or item.get("itemDescription", "")
    if description:
        result["description"] = description
    else:
        meta_el = page.css("meta[name='description']")
        if meta_el:
            result["description"] = str(meta_el[0].attrib.get("content", "") or "")

    image_urls = []
    for key in ("images", "image", "imageList", "thumbnails"):
        imgs = item.get(key)
        if imgs is None:
            continue
        if isinstance(imgs, str) and imgs.startswith("http"):
            if imgs not in image_urls:
                image_urls.append(imgs)
        elif isinstance(imgs, list):
            for img in imgs:
                if isinstance(img, str) and img.startswith("http") and img not in image_urls:
                    image_urls.append(img)
                elif isinstance(img, dict):
                    img_url = img.get("url") or img.get("src") or img.get("imageUrl")
                    if img_url and img_url.startswith("http") and img_url not in image_urls:
                        image_urls.append(img_url)
        elif isinstance(imgs, dict):
            img_url = imgs.get("url") or imgs.get("src") or imgs.get("imageUrl")
            if img_url and img_url.startswith("http") and img_url not in image_urls:
                image_urls.append(img_url)
    if not image_urls:
        og_el = page.css("meta[property='og:image']")
        if og_el:
            og_url = str(og_el[0].attrib.get("content", "") or "")
            if og_url.startswith("http"):
                image_urls.append(og_url)
    result["image_urls"] = image_urls

    status_flag = item.get("status") or item.get("soldOut") or item.get("isSoldOut")
    if status_flag in (True, "sold_out", "soldout", "SOLD_OUT"):
        result["status"] = "sold"
    else:
        page_text = str(page.get_all_text())
        if "SOLD OUT" in page_text or "売り切れ" in page_text or "在庫なし" in page_text:
            result["status"] = "sold"

    return result


def scrape_item_detail_light(url: str) -> dict:
    """
    Static-first SNKRDUNK detail scrape using the embedded __NEXT_DATA__ JSON.
    """
    try:
        from services.scraping_client import fetch_dynamic, fetch_static

        try:
            page = fetch_static(url)
        except Exception as exc:
            logger.debug("SNKRDUNK static detail fetch failed, retrying dynamic fetch: %s", exc)
            page = fetch_dynamic(url, headless=True, network_idle=True)
            return _parse_detail_page(page, url)

        result = _parse_detail_page(page, url)
        if result.get("title"):
            return result

        logger.debug("SNKRDUNK static detail parse incomplete, retrying dynamic fetch")
        page = fetch_dynamic(url, headless=True, network_idle=True)
        return _parse_detail_page(page, url)
    except Exception as exc:
        logger.debug("SNKRDUNK light scrape error: %s", exc)
        return {}


def scrape_item_detail(url_or_driver, maybe_url=None, **_kwargs):
    """
    SNKRDUNK detail scrape.
    The legacy `(driver, url)` signature is accepted for backward compatibility.
    """
    url = _resolve_detail_url(url_or_driver, maybe_url)
    return scrape_item_detail_light(url) or _empty_result(url)


def scrape_single_item(url: str, headless: bool = True):
    """Scrape a single SNKRDUNK product and return `list[dict]`."""
    metrics = get_metrics()
    metrics.start("snkrdunk", "single")
    try:
        data = scrape_item_detail(url)
        log_scrape_result("snkrdunk", url, data)
        if data.get("title"):
            metrics.finish()
            return [data]
        metrics.record_attempt(False, url, "empty title")
        metrics.finish()
        return []
    except Exception as exc:
        metrics.record_attempt(False, url, str(exc))
        metrics.finish()
        logger.error("SNKRDUNK single scrape error: %s", exc)
        return []


def _extract_search_urls(page, base_url: str, max_items: int) -> list:
    link_selectors = get_selectors("snkrdunk", "search", "item_links") or [
        "a[class*='resultProductTile']",
        "a[class*='productTile']",
        "a[href*='/products/']",
    ]
    valid_domains = get_valid_domains("snkrdunk", "search") or ["snkrdunk.com"]

    urls = []
    seen = set()
    for selector in link_selectors:
        for anchor in page.css(selector):
            href = str(anchor.attrib.get("href", "") or "").strip()
            if not href:
                continue
            full_url = urljoin(base_url, href)
            if not any(domain in full_url for domain in valid_domains):
                continue
            if "/products/" not in full_url:
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            urls.append(full_url)
            if len(urls) >= max_items:
                return urls
    return urls


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
):
    """Scrape SNKRDUNK search results without Selenium."""
    metrics = get_metrics()
    metrics.start("snkrdunk", "search")
    items = []

    try:
        from services.scraping_client import fetch_dynamic, fetch_static

        try:
            page = fetch_dynamic(search_url, headless=headless, network_idle=True)
        except Exception as exc:
            logger.debug("SNKRDUNK dynamic search fetch failed, retrying static fetch: %s", exc)
            page = fetch_static(search_url)

        candidate_urls = _extract_search_urls(page, search_url, max_items=max_items * 2)
        for item_url in candidate_urls[:max_items]:
            data = scrape_item_detail(item_url)
            log_scrape_result("snkrdunk", item_url, data)
            if data.get("title"):
                items.append(data)
            else:
                metrics.record_attempt(False, item_url, "empty title")

        health = check_scrape_health(items)
        if health["action_required"]:
            logger.warning("SNKRDUNK scrape health check: %s", health["message"])
        metrics.finish()
        return items
    except Exception as exc:
        metrics.record_attempt(False, search_url, str(exc))
        metrics.finish()
        logger.error("SNKRDUNK search scrape error: %s", exc)
        return []
