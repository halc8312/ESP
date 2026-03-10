"""
Yahoo Auctions scraping module.
Uses Scrapling HTTP fetches for product detail pages and search results.
"""
import json
import logging
import re
from urllib.parse import urljoin

logger = logging.getLogger("yahuoku")


SEARCH_LINK_SELECTORS = [
    ".Product__titleLink",
    "a[href*='/auction/']",
    "a[href*='page.auctions.yahoo.co.jp/auction/']",
]


def _empty_result(url: str, status: str = "error") -> dict:
    return {
        "url": url,
        "title": "",
        "price": None,
        "status": status,
        "description": "",
        "image_urls": [],
        "variants": [],
        "auction_id": "",
        "seller": "",
        "end_time": "",
    }


def _resolve_detail_url(url_or_driver, maybe_url=None) -> str:
    if isinstance(maybe_url, str) and maybe_url:
        return maybe_url
    if isinstance(url_or_driver, str) and url_or_driver:
        return url_or_driver
    raise ValueError("url is required")


def _extract_auction_item(page) -> dict:
    script_el = page.find("#__NEXT_DATA__")
    if not script_el:
        return {}

    json_str = str(script_el.text or "").strip()
    if not json_str:
        return {}

    data = json.loads(json_str)
    props = data.get("props", {})
    page_props = props.get("pageProps", {})

    initial_state = page_props.get("initialState", {})
    item_detail = initial_state.get("item", {}).get("detail", {}).get("item", {})
    if item_detail:
        return item_detail

    initial_props = page_props.get("initialProps", {})
    return initial_props.get("auctionItem", {}) or {}


def scrape_item_detail_light(url: str) -> dict:
    """HTTP-only Yahoo Auctions detail scrape."""
    try:
        from services.scraping_client import fetch_static

        page = fetch_static(url)
        item_detail = _extract_auction_item(page)
        if not item_detail:
            return {}

        result = _empty_result(url, status="active")
        result["title"] = item_detail.get("title", "")

        price_data = item_detail.get("price", {})
        if isinstance(price_data, dict):
            result["price"] = price_data.get("current") or price_data.get("bid")
        elif isinstance(price_data, (int, float)):
            result["price"] = int(price_data)
        if result["price"] is None:
            result["price"] = item_detail.get("currentPrice") or item_detail.get("price")

        description = item_detail.get("description", "") or item_detail.get("itemDescription", "")
        if description:
            result["description"] = description
        else:
            meta_el = page.css("meta[name='description']")
            if meta_el:
                result["description"] = str(meta_el[0].attrib.get("content", "") or "")

        image_urls = []
        for key in ("images", "image", "imageList"):
            imgs = item_detail.get(key)
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
                        img_url = img.get("url") or img.get("src") or img.get("image") or img.get("imageUrl")
                        if img_url and img_url.startswith("http") and img_url not in image_urls:
                            image_urls.append(img_url)
            elif isinstance(imgs, dict):
                img_url = imgs.get("url") or imgs.get("src") or imgs.get("image") or imgs.get("imageUrl")
                if img_url and img_url.startswith("http") and img_url not in image_urls:
                    image_urls.append(img_url)
        if not image_urls:
            og_el = page.css("meta[property='og:image']")
            if og_el:
                og_url = str(og_el[0].attrib.get("content", "") or "")
                if og_url.startswith("http"):
                    image_urls.append(og_url)
        result["image_urls"] = image_urls

        status_flag = item_detail.get("status") or item_detail.get("isFinished") or item_detail.get("isClosed")
        if status_flag in (True, "closed", "finished", "ended"):
            result["status"] = "sold"
        else:
            page_text = str(page.get_all_text())
            if "終了" in page_text or "落札" in page_text:
                result["status"] = "sold"

        seller_data = item_detail.get("seller", {})
        if isinstance(seller_data, dict):
            result["seller"] = seller_data.get("name", "")

        match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
        if match:
            result["auction_id"] = match.group(1)
        result["auction_id"] = item_detail.get("auctionID", result["auction_id"])

        if result["price"]:
            result["variants"] = [
                {
                    "option1_value": "Default Title",
                    "price": result["price"],
                    "sku": result["auction_id"],
                    "inventory_qty": 1 if result["status"] == "active" else 0,
                }
            ]

        return result
    except Exception as exc:
        logger.debug("Yahuoku light scrape error: %s", exc)
        return {}


def scrape_item_detail(url_or_driver, maybe_url=None, **_kwargs) -> dict:
    """
    Yahoo Auctions detail scrape.
    The legacy `(driver, url)` signature is accepted for backward compatibility.
    """
    url = _resolve_detail_url(url_or_driver, maybe_url)
    return scrape_item_detail_light(url) or _empty_result(url)


def scrape_single_item(url: str, headless: bool = True) -> list:
    """Scrape a single Yahoo Auctions item and return `list[dict]`."""
    result = scrape_item_detail(url)
    return [result] if result.get("title") else []


def _extract_search_urls(page, base_url: str, max_items: int) -> list:
    urls = []
    seen = set()
    for selector in SEARCH_LINK_SELECTORS:
        for anchor in page.css(selector):
            href = str(anchor.attrib.get("href", "") or "").strip()
            if not href:
                continue
            full_url = urljoin(base_url, href)
            if "/auction/" not in full_url:
                continue
            if not (
                "auctions.yahoo.co.jp" in full_url
                or "page.auctions.yahoo.co.jp" in full_url
            ):
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
    """Scrape Yahoo Auctions search results using HTTP-only page fetches."""
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
