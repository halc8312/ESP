"""
Yahoo Shopping scraping module.
Uses Scrapling HTTP fetches for product detail pages and search results.
"""
import json
import logging
from urllib.parse import urljoin

from selector_config import get_selectors, get_valid_domains
from scrape_metrics import check_scrape_health, get_metrics, log_scrape_result

logger = logging.getLogger("yahoo")


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


def _extract_item_from_page(page) -> dict:
    script_el = page.find("#__NEXT_DATA__")
    if not script_el:
        return {}

    json_str = str(script_el.text or "").strip()
    if not json_str:
        return {}

    data = json.loads(json_str)
    page_props = data.get("props", {}).get("pageProps", {})
    return (
        page_props.get("item")
        or page_props.get("sp", {}).get("item", {})
        or page_props.get("initialState", {}).get("item", {})
        or {}
    )


def scrape_item_detail_light(url: str) -> dict:
    """
    HTTP-only Yahoo Shopping detail scrape.
    Returns an empty dict when the page structure cannot be parsed.
    """
    result = _empty_result(url, status="on_sale")
    try:
        from services.scraping_client import fetch_static

        page = fetch_static(url)
        item = _extract_item_from_page(page)
        if not item:
            logger.debug("No JSON item data found, falling back to CSS selectors")
            title_selectors = get_selectors("yahoo", "detail", "title") or ["[class*='styles_name__']", "h1"]
            for sel in title_selectors:
                els = page.css(sel)
                if els and els[0].text:
                    result["title"] = str(els[0].text).strip()
                    break
            
            if not result.get("title"):
                return {}
                
            price_selectors = get_selectors("yahoo", "detail", "price") or ["[class*='styles_price__']", ".price"]
            for sel in price_selectors:
                els = page.css(sel)
                if els and els[0].text:
                    price_str = str(els[0].text).strip()
                    digits = ''.join(c for c in price_str if c.isdigit())
                    if digits:
                        result["price"] = int(digits)
                        break

            desc_selectors = get_selectors("yahoo", "detail", "description") or ["[class*='styles_content__']", ".description"]
            for sel in desc_selectors:
                els = page.css(sel)
                if els and els[0].text:
                    result["description"] = str(els[0].text).strip()
                    break

            image_selectors = get_selectors("yahoo", "detail", "images") or ["img"]
            image_urls = []
            for sel in image_selectors:
                els = page.css(sel)
                for el in els:
                    src = el.attrib.get("src") or el.attrib.get("data-src")
                    if src and src.startswith("http") and src not in image_urls:
                        image_urls.append(str(src))
                if image_urls:
                    break
            result["image_urls"] = image_urls
            
            return result

        if item.get("name"):
            result["title"] = item["name"]

        if item.get("applicablePrice"):
            result["price"] = int(item["applicablePrice"])
        elif item.get("price"):
            result["price"] = int(item["price"])

        image_urls = []
        json_images = item.get("images")
        if isinstance(json_images, dict):
            image_list = []
            for key in ("list", "itemImageList", "detailImageList"):
                image_list.extend(json_images.get(key, []))
            if "mainImage" in json_images:
                image_list.append(json_images["mainImage"])
            for img in image_list:
                if isinstance(img, dict):
                    img_url = img.get("src") or img.get("url") or img.get("path")
                    if not img_url and img.get("id"):
                        img_url = f"https://item-shopping.c.yimg.jp/i/n/{img['id']}"
                elif isinstance(img, str):
                    img_url = img
                else:
                    img_url = ""
                if img_url and img_url.startswith("http") and img_url not in image_urls:
                    image_urls.append(img_url)
        result["image_urls"] = image_urls

        variants = []
        spec_list = item.get("specList", [])
        base_price = result["price"]

        if isinstance(item.get("stockTableTwoAxis"), dict):
            two_axis = item["stockTableTwoAxis"]
            first_opt = two_axis.get("firstOption", {})
            axis1_label = first_opt.get("name") or (
                spec_list[1].get("name") if len(spec_list) > 1 else "Option 1"
            )
            for opt1 in first_opt.get("choiceList", []):
                v_name1 = opt1.get("choiceName")
                sec_opt = opt1.get("secondOption", {})
                axis2_label = sec_opt.get("name") or (
                    spec_list[0].get("name") if len(spec_list) > 0 else "Option 2"
                )
                for opt2 in sec_opt.get("choiceList", []):
                    v_name2 = opt2.get("choiceName")
                    stock_info = opt2.get("stock", {})
                    qty = stock_info.get("quantity", 0)
                    v_price = opt2.get("price") or base_price
                    if v_name1 and v_name2:
                        variants.append(
                            {
                                "option1_name": axis1_label,
                                "option1_value": v_name1,
                                "option2_name": axis2_label,
                                "option2_value": v_name2,
                                "price": v_price,
                                "inventory_qty": qty,
                            }
                        )
        elif isinstance(item.get("stockTableOneAxis"), dict):
            one_axis = item["stockTableOneAxis"]
            first_opt = one_axis.get("firstOption", {})
            axis1_label = first_opt.get("name") or (
                spec_list[0].get("name") if len(spec_list) > 0 else "Option 1"
            )
            for opt1 in first_opt.get("choiceList", []):
                v_name1 = opt1.get("choiceName")
                stock_info = opt1.get("stock", {})
                qty = stock_info.get("quantity", 0)
                v_price = opt1.get("price") or base_price
                if v_name1:
                    variants.append(
                        {
                            "option1_name": axis1_label,
                            "option1_value": v_name1,
                            "price": v_price,
                            "inventory_qty": qty,
                        }
                    )
        result["variants"] = variants

        description = item.get("description", "") or item.get("itemDescription", "")
        if description:
            result["description"] = description
        else:
            meta_el = page.css("meta[name='description']")
            if meta_el:
                result["description"] = str(meta_el[0].attrib.get("content", "") or "")

        stock = item.get("stock", {})
        if isinstance(stock, dict):
            qty = stock.get("quantity")
            if stock.get("isSoldOut") or (qty is not None and qty <= 0):
                result["status"] = "sold"

        return result
    except Exception as exc:
        logger.debug("Yahoo light scrape error: %s", exc)
        return {}


def scrape_item_detail(url_or_driver, maybe_url=None, **_kwargs):
    """
    Yahoo Shopping detail scrape.
    The legacy `(driver, url)` signature is accepted for backward compatibility.
    """
    url = _resolve_detail_url(url_or_driver, maybe_url)
    return scrape_item_detail_light(url) or _empty_result(url)


def _extract_search_urls(page, base_url: str, max_items: int) -> list:
    item_link_selectors = get_selectors("yahoo", "search", "item_links") or [
        "a[class*='SearchResult_SearchResultItem__detailLink']",
        "a[class*='ItemImageLink']",
        "li.LoopList__item a",
        ".Item__title a",
        "[data-testid='item-name'] a",
    ]
    valid_domains = get_valid_domains("yahoo", "search") or [
        "store.shopping.yahoo.co.jp",
        "shopping-item-reach.yahoo.co.jp",
    ]

    urls = []
    seen = set()
    for selector in item_link_selectors:
        for anchor in page.css(selector):
            href = str(anchor.attrib.get("href", "") or "").strip()
            if not href:
                continue
            full_url = urljoin(base_url, href)
            
            if "shopping-item-reach.yahoo.co.jp" in full_url and "rdUrl=" in full_url:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(full_url)
                qs = parse_qs(parsed.query)
                if "rdUrl" in qs:
                    full_url = qs["rdUrl"][0]

            if not any(domain in full_url for domain in valid_domains):
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
        rel = str(anchor.attrib.get("rel", "") or "")
        if "次へ" in text or "elNext" in classes or rel == "next":
            return urljoin(current_url, href)
    return ""


def scrape_single_item(url: str, headless: bool = True):
    """One-shot Yahoo Shopping scrape returning `list[dict]`."""
    metrics = get_metrics()
    metrics.start("yahoo", "single")
    try:
        data = scrape_item_detail(url)
        log_scrape_result("yahoo", url, data)
        if data.get("title"):
            metrics.finish()
            return [data]
        metrics.record_attempt(False, url, "empty title")
        metrics.finish()
        return []
    except Exception as exc:
        metrics.record_attempt(False, url, str(exc))
        metrics.finish()
        logger.error("Yahoo single scrape error: %s", exc)
        return []


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
):
    """Yahoo Shopping search scrape using HTTP-only page fetches."""
    metrics = get_metrics()
    metrics.start("yahoo", "search")
    items = []
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
            data = scrape_item_detail(item_url)
            log_scrape_result("yahoo", item_url, data)
            if data.get("title"):
                items.append(data)
            else:
                metrics.record_attempt(False, item_url, "empty title")

        health = check_scrape_health(items)
        if health["action_required"]:
            logger.warning("Yahoo scrape health check: %s", health["message"])
        metrics.finish()
        return items
    except Exception as exc:
        metrics.record_attempt(False, search_url, str(exc))
        metrics.finish()
        logger.error("Yahoo search scrape error: %s", exc)
        return []
