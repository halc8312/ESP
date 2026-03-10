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


def scrape_item_detail_light(url: str) -> dict:
    """
    HTTP-only SNKRDUNK detail scrape using the embedded __NEXT_DATA__ JSON.
    """
    try:
        from services.scraping_client import fetch_static

        page = fetch_static(url)
        script_el = page.find("#__NEXT_DATA__")
        if not script_el:
            return {}

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

        result = _empty_result(url, status="on_sale")
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
