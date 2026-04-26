"""
SNKRDUNK scraping module.
Uses Scrapling HTTP fetches for detail pages and Scrapling dynamic fetches for search pages.
"""
import asyncio
import json
import logging
import re
from urllib.parse import urljoin

from selector_config import get_selectors, get_valid_domains
from services.detail_field_strategy_runner import DetailFieldStrategy, run_detail_field_strategies
from services.scrape_alerts import report_detail_result
from services.snkrdunk_browser_fetch import (
    fetch_snkrdunk_page_via_browser_pool_sync,
    should_use_snkrdunk_browser_pool_dynamic,
)
from services.extraction_policy import attach_extraction_trace
from scrape_metrics import check_scrape_health, get_metrics, log_scrape_result
from services.selector_healer import get_healer
from services.scraping_client import run_coro_sync

logger = logging.getLogger("snkrdunk")


_SNKRDUNK_SOLD_PAGE_MARKERS = (
    "SOLD OUT",
    "売り切れ",
    "在庫なし",
    "現在出品はありません",
)
_SNKRDUNK_ACTIVE_PAGE_MARKERS = (
    "購入する",
    "カートに入れる",
    "今すぐ購入",
)
_SNKRDUNK_ACTIVE_STATUS_VALUES = {
    "active",
    "selling",
    "open",
    "available",
    "instock",
    "in_stock",
    "on_sale",
}
_SNKRDUNK_SOLD_STATUS_VALUES = {
    "sold",
    "sold_out",
    "soldout",
    "outofstock",
    "out_of_stock",
    "discontinued",
    "closed",
    "unavailable",
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
    }


def _resolve_detail_url(url_or_driver, maybe_url=None) -> str:
    if isinstance(maybe_url, str) and maybe_url:
        return maybe_url
    if isinstance(url_or_driver, str) and url_or_driver:
        return url_or_driver
    raise ValueError("url is required")


def _is_nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_image_urls(value) -> bool:
    return isinstance(value, list) and len(value) > 0


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


def _extract_unique_price_from_page_text(page_text: str):
    if not page_text:
        return None

    matches = re.findall(r"[¥￥]\s*([\d,]+)|([\d,]+)\s*円", page_text)
    unique_prices = set()
    for yen_match, yen_suffix_match in matches:
        digits = (yen_match or yen_suffix_match or "").replace(",", "")
        if not digits:
            continue
        try:
            unique_prices.add(int(digits))
        except ValueError:
            continue

    if len(unique_prices) == 1:
        return next(iter(unique_prices))
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


def _infer_snkrdunk_status(item: dict | None, page_text: str = "", availability: str = "") -> tuple[str, str]:
    item = item or {}

    raw_status = item.get("status")
    normalized_status = str(raw_status or "").strip().lower()
    if normalized_status in _SNKRDUNK_ACTIVE_STATUS_VALUES:
        return "on_sale", "next_data"
    if normalized_status in _SNKRDUNK_SOLD_STATUS_VALUES:
        return "sold", "next_data"

    sold_flag = item.get("soldOut")
    if sold_flag in (True, "sold_out", "soldout", "SOLD_OUT"):
        return "sold", "next_data"

    if item.get("isSoldOut") is True:
        return "sold", "next_data"
    if item.get("isOnSale") is True:
        return "on_sale", "next_data"

    availability_lower = str(availability or "").strip().lower()
    if "outofstock" in availability_lower:
        return "sold", "json_ld"
    if "instock" in availability_lower:
        return "on_sale", "json_ld"

    if any(marker in page_text for marker in _SNKRDUNK_SOLD_PAGE_MARKERS):
        return "sold", "css"
    if any(marker in page_text for marker in _SNKRDUNK_ACTIVE_PAGE_MARKERS):
        return "on_sale", "css"

    return "unknown", ""


def _normalize_snkrdunk_title(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"の新品/中古.*$", "", normalized).strip()
    normalized = re.sub(r"\s*[|｜]\s*スニダン\s*$", "", normalized).strip()
    return normalized


def _parse_detail_page(page, url: str) -> dict:
    result = _empty_result(url, status="unknown")
    field_sources = {}
    page_text = str(page.get_all_text() or "")
    script_el = page.find("#__NEXT_DATA__")
    data = None

    if script_el:
        json_str = str(script_el.text or "").strip()
        if json_str:
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                data = None
    else:
        logger.debug("SNKRDUNK __NEXT_DATA__ not found (site may have migrated to App Router)")

    if data is not None:
        props = data.get("props", {})
        page_props = props.get("pageProps", {})
        item = (
            page_props.get("item")
            or page_props.get("product")
            or page_props.get("initialState", {}).get("item", {})
            or page_props.get("initialState", {}).get("product", {})
            or {}
        )
        if item:
            meta_title = _normalize_snkrdunk_title(
                _get_first_meta_content(page, ["meta[property='og:title']", "meta[name='twitter:title']"])
                or _get_first_text(page, ["title"])
            )
            title, title_source = run_detail_field_strategies(
                DetailFieldStrategy("next_data", item.get("name") or item.get("title") or item.get("productName", "")),
                DetailFieldStrategy("meta", meta_title),
                validator=_is_nonempty_text,
                default="",
            )
            if title:
                result["title"] = title
                field_sources["title"] = title_source

            price_raw = item.get("price") or item.get("lowestPrice") or item.get("minPrice")
            if price_raw is not None:
                try:
                    result["price"] = int(price_raw)
                    field_sources["price"] = "next_data"
                except (ValueError, TypeError):
                    pass

            meta_description = _get_first_meta_content(page, ["meta[name='description']", "meta[property='og:description']"])
            description, description_source = run_detail_field_strategies(
                DetailFieldStrategy("next_data", item.get("description") or item.get("itemDescription", "")),
                DetailFieldStrategy("meta", meta_description),
                validator=_is_nonempty_text,
                default="",
            )
            if description:
                result["description"] = description
                field_sources["description"] = description_source

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
            image_urls, image_source = run_detail_field_strategies(
                DetailFieldStrategy("next_data", image_urls),
                DetailFieldStrategy(
                    "meta",
                    resolver=lambda: _collect_image_urls(_get_first_meta_content(page, ["meta[property='og:image']"])),
                ),
                validator=_has_image_urls,
                default=[],
            )
            result["image_urls"] = image_urls
            if image_source:
                field_sources["images"] = image_source

            status, status_source = _infer_snkrdunk_status(item, page_text)
            result["status"] = status
            if status_source:
                field_sources["status"] = status_source

            if result.get("title"):
                return attach_extraction_trace(result, strategy="next_data", field_sources=field_sources)

    product_jsonld = _extract_product_jsonld(page)
    if product_jsonld:
        meta_title = _normalize_snkrdunk_title(
            _get_first_meta_content(page, ["meta[property='og:title']", "meta[name='twitter:title']"])
            or _get_first_text(page, ["title"])
        )
        title, title_source = run_detail_field_strategies(
            DetailFieldStrategy("json_ld", str(product_jsonld.get("name") or "").strip()),
            DetailFieldStrategy("meta", meta_title),
            validator=_is_nonempty_text,
            default="",
        )
        if title:
            result["title"] = title
            field_sources["title"] = title_source

        price = _extract_jsonld_price(product_jsonld.get("offers"))
        if price is not None:
            result["price"] = price
            field_sources["price"] = "json_ld"

        description, description_source = run_detail_field_strategies(
            DetailFieldStrategy("json_ld", str(product_jsonld.get("description") or "").strip()),
            DetailFieldStrategy(
                "meta",
                resolver=lambda: _get_first_meta_content(page, ["meta[name='description']", "meta[property='og:description']"]),
            ),
            validator=_is_nonempty_text,
            default="",
        )
        result["description"] = description or ""
        if description_source:
            field_sources["description"] = description_source

        image_urls, image_source = run_detail_field_strategies(
            DetailFieldStrategy("json_ld", _collect_image_urls(product_jsonld.get("image"))),
            DetailFieldStrategy(
                "meta",
                resolver=lambda: _collect_image_urls(_get_first_meta_content(page, ["meta[property='og:image']"])),
            ),
            validator=_has_image_urls,
            default=[],
        )
        result["image_urls"] = image_urls
        if image_source:
            field_sources["images"] = image_source

        availability = _extract_jsonld_availability(product_jsonld.get("offers"))
        status, status_source = _infer_snkrdunk_status({}, page_text, availability)
        result["status"] = status
        if status_source:
            field_sources["status"] = status_source

        if result.get("title"):
            return attach_extraction_trace(result, strategy="json_ld", field_sources=field_sources)

    logger.debug("No structured product data found, falling back to meta/CSS selectors")
    healer = get_healer()

    meta_title = _normalize_snkrdunk_title(
        _get_first_meta_content(page, ["meta[property='og:title']", "meta[name='twitter:title']"])
        or _get_first_text(page, ["title"])
    )
    title_val, _ = healer.extract_with_healing(page, 'snkrdunk', 'detail', 'title', parser='scrapling')
    title, title_source = run_detail_field_strategies(
        DetailFieldStrategy("meta", meta_title),
        DetailFieldStrategy("css", title_val),
        validator=_is_nonempty_text,
        default="",
    )
    if title:
        result["title"] = title
        field_sources["title"] = title_source

    if not result.get("title"):
        # Try JP title as well
        jp_title_nodes = page.css("h2.product-name-ja, p.product-name-jp")
        if jp_title_nodes:
            jp_title = str(jp_title_nodes[0].text or "").strip()
            if jp_title:
                result["title"] = jp_title
                field_sources["title"] = "css"

    if not result.get("title"):
        return {}

    price_val, _ = healer.extract_with_healing(page, 'snkrdunk', 'detail', 'price', parser='scrapling')
    if price_val:
        result["price"] = _extract_price_value(price_val)
        if result["price"] is not None:
            field_sources["price"] = "css"
    if result["price"] is None:
        result["price"] = _extract_unique_price_from_page_text(page.get_all_text() or "")
        if result["price"] is not None:
            field_sources["price"] = "css"

    meta_description = _get_first_meta_content(page, ["meta[name='description']", "meta[property='og:description']"])
    desc_val, _ = healer.extract_with_healing(page, 'snkrdunk', 'detail', 'description', parser='scrapling')
    description, description_source = run_detail_field_strategies(
        DetailFieldStrategy("meta", meta_description),
        DetailFieldStrategy("css", desc_val),
        validator=_is_nonempty_text,
        default="",
    )
    if description:
        result["description"] = description
        field_sources["description"] = description_source

    css_images, _ = healer.extract_images_with_healing(page, 'snkrdunk', 'detail', parser='scrapling')
    image_urls, image_source = run_detail_field_strategies(
        DetailFieldStrategy(
            "meta",
            resolver=lambda: _collect_image_urls(_get_first_meta_content(page, ["meta[property='og:image']"])),
        ),
        DetailFieldStrategy("css", css_images),
        validator=_has_image_urls,
        default=[],
    )
    result["image_urls"] = image_urls
    if image_source:
        field_sources["images"] = image_source

    status, status_source = _infer_snkrdunk_status({}, page_text)
    result["status"] = status
    if status_source:
        field_sources["status"] = status_source

    strategy = "meta" if any(source == "meta" for source in field_sources.values()) else "css"
    return attach_extraction_trace(result, strategy=strategy, field_sources=field_sources)


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
            if should_use_snkrdunk_browser_pool_dynamic():
                page = fetch_snkrdunk_page_via_browser_pool_sync(url, network_idle=True)
            else:
                page = fetch_dynamic(url, headless=True, network_idle=True)
            return _parse_detail_page(page, url)

        result = _parse_detail_page(page, url)
        if result.get("title"):
            return result

        logger.debug("SNKRDUNK static detail parse incomplete, retrying dynamic fetch")
        if should_use_snkrdunk_browser_pool_dynamic():
            page = fetch_snkrdunk_page_via_browser_pool_sync(url, network_idle=True)
        else:
            page = fetch_dynamic(url, headless=True, network_idle=True)
        return _parse_detail_page(page, url)
    except Exception as exc:
        logger.debug("SNKRDUNK light scrape error: %s", exc)
        return {}


async def _scrape_item_detail_async(url: str) -> dict:
    from services.scraping_client import fetch_dynamic, fetch_static_async, get_async_fetch_settings

    settings = get_async_fetch_settings("snkrdunk")

    try:
        try:
            page = await fetch_static_async(
                url,
                timeout=settings.timeout,
                retries=settings.retries,
                backoff_seconds=settings.backoff_seconds,
            )
        except Exception as exc:
            logger.debug("SNKRDUNK async static detail fetch failed, retrying dynamic fetch: %s", exc)
            if should_use_snkrdunk_browser_pool_dynamic():
                page = await asyncio.to_thread(fetch_snkrdunk_page_via_browser_pool_sync, url, network_idle=True)
            else:
                page = await asyncio.to_thread(fetch_dynamic, url, headless=True, network_idle=True)
            return _parse_detail_page(page, url)

        result = _parse_detail_page(page, url)
        if result.get("title"):
            return result

        logger.debug("SNKRDUNK async static detail parse incomplete, retrying dynamic fetch")
        if should_use_snkrdunk_browser_pool_dynamic():
            page = await asyncio.to_thread(fetch_snkrdunk_page_via_browser_pool_sync, url, network_idle=True)
        else:
            page = await asyncio.to_thread(fetch_dynamic, url, headless=True, network_idle=True)
        return _parse_detail_page(page, url)
    except Exception as exc:
        logger.debug("SNKRDUNK async detail scrape error: %s", exc)
        return {}


def scrape_item_detail(url_or_driver, maybe_url=None, **_kwargs):
    """
    SNKRDUNK detail scrape.
    The legacy `(driver, url)` signature is accepted for backward compatibility.
    """
    url = _resolve_detail_url(url_or_driver, maybe_url)
    result = scrape_item_detail_light(url) or _empty_result(url)
    report_detail_result("snkrdunk", url, result, result.get("_scrape_meta"), page_type="detail")
    return result


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


def _find_next_page_url(page, current_url: str) -> str:
    for anchor in page.css("a[href]"):
        href = str(anchor.attrib.get("href", "") or "").strip()
        if not href:
            continue

        full_url = urljoin(current_url, href)
        if full_url == current_url or "snkrdunk.com" not in full_url:
            continue

        text = str(anchor.text or "").strip().lower()
        classes = str(anchor.attrib.get("class", "") or "").lower()
        rel = str(anchor.attrib.get("rel", "") or "").lower()
        aria_label = str(anchor.attrib.get("aria-label", "") or "").lower()

        if (
            "次へ" in text
            or "next" in text
            or "next" in classes
            or "next" in rel
            or "next" in aria_label
        ):
            return full_url
    return ""


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
    candidate_urls = []
    candidate_target = max(max_items, max_items * 2)

    try:
        from services.scraping_client import fetch_dynamic, fetch_static, gather_with_concurrency, get_async_fetch_settings

        current_url = search_url
        seen_pages = set()
        max_pages = max(1, max_scroll)

        while current_url and current_url not in seen_pages and len(seen_pages) < max_pages:
            seen_pages.add(current_url)
            if should_use_snkrdunk_browser_pool_dynamic():
                try:
                    page = fetch_snkrdunk_page_via_browser_pool_sync(current_url, network_idle=True)
                except Exception as exc:
                    logger.debug("SNKRDUNK browser-pool search fetch failed, retrying static fetch: %s", exc)
                    page = fetch_static(current_url)
            else:
                try:
                    page = fetch_dynamic(current_url, headless=headless, network_idle=True)
                except Exception as exc:
                    logger.debug("SNKRDUNK dynamic search fetch failed, retrying static fetch: %s", exc)
                    page = fetch_static(current_url)

            for item_url in _extract_search_urls(page, current_url, max_items=candidate_target):
                if item_url not in candidate_urls:
                    candidate_urls.append(item_url)
                if len(candidate_urls) >= candidate_target:
                    break
            if len(candidate_urls) >= candidate_target:
                break
            current_url = _find_next_page_url(page, current_url)

        settings = get_async_fetch_settings("snkrdunk")
        detail_results = run_coro_sync(
            gather_with_concurrency(
                candidate_urls,
                _scrape_item_detail_async,
                concurrency=settings.concurrency,
            )
        )

        for item_url, data in zip(candidate_urls, detail_results):
            if len(items) >= max_items:
                break
            if isinstance(data, Exception):
                metrics.record_attempt(False, item_url, str(data))
                continue
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
