"""
Yahoo Auctions scraping module.
Uses Scrapling HTTP fetches for product detail pages and search results.
"""
import json
import logging
import re
from urllib.parse import urljoin

from services.extraction_policy import attach_extraction_trace, pick_first
from services.scrape_alerts import report_detail_result

logger = logging.getLogger("yahuoku")


SEARCH_LINK_SELECTORS = [
    ".Product__titleLink",
    "a[href*='/auction/']",
    "a[href*='page.auctions.yahoo.co.jp/auction/']",
]

_CLOSED_STATUS_VALUES = {
    "closed",
    "finished",
    "ended",
    "closedbysystem",
    "closedbyseller",
    "sold",
}
_CLOSED_PAGE_MARKERS = (
    "このオークションは終了しています",
    "オークションは終了しました",
    "落札されました",
    "落札価格",
)
_OPEN_PAGE_MARKERS = (
    "入札する",
    "今すぐ落札",
    "購入手続きへ",
)


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


def _extract_price_value(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return int(raw_value)

    digits = "".join(ch for ch in str(raw_value) if ch.isdigit())
    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def _extract_tax_inclusive_price(item_detail: dict, page_text: str = ""):
    for key in ("taxinPrice", "taxinBidorbuy", "taxinStartPrice"):
        price = _extract_price_value(item_detail.get(key))
        if price is not None and price > 0:
            return price

    price_data = item_detail.get("price", {})
    if isinstance(price_data, dict):
        for key in ("taxInCurrent", "taxInBid", "taxinCurrent", "taxinBid"):
            price = _extract_price_value(price_data.get(key))
            if price is not None and price > 0:
                return price

    if page_text:
        for pattern in (
            r"価格\s*([\d,]+)円\s*（税込）",
            r"即決\s*([\d,]+)円\s*（税込）",
        ):
            match = re.search(pattern, page_text)
            if not match:
                continue
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:
                continue

    if isinstance(price_data, dict):
        for key in ("current", "bid"):
            price = _extract_price_value(price_data.get(key))
            if price is not None:
                return price
    elif isinstance(price_data, (int, float)):
        return int(price_data)

    for key in ("currentPrice", "price", "initPrice"):
        price = _extract_price_value(item_detail.get(key))
        if price is not None:
            return price

    return None


def _infer_auction_status(item_detail: dict, page_text: str = "") -> str:
    status_flag = item_detail.get("status")
    close_status = item_detail.get("closeStatus")

    for raw_value in (status_flag, close_status):
        if raw_value in (True, False):
            if raw_value is True:
                return "sold"
            continue
        normalized = str(raw_value or "").strip().lower()
        if not normalized:
            continue
        if normalized in _CLOSED_STATUS_VALUES:
            return "sold"
        if normalized in {"open", "active", "selling"}:
            return "active"

    if item_detail.get("isFinished") is True or item_detail.get("isClosed") is True:
        return "sold"
    if item_detail.get("isEndValid") is True:
        return "sold"

    if any(marker in page_text for marker in _CLOSED_PAGE_MARKERS):
        return "sold"
    if any(marker in page_text for marker in _OPEN_PAGE_MARKERS):
        return "active"

    return "unknown"


def scrape_item_detail_light(url: str) -> dict:
    """HTTP-only Yahoo Auctions detail scrape."""
    try:
        from services.scraping_client import fetch_static

        page = fetch_static(url)
        item_detail = _extract_auction_item(page)
        if not item_detail:
            return {}

        result = _empty_result(url, status="unknown")
        field_sources = {}
        meta_title = next(
            (str(el.attrib.get("content", "") or "") for el in page.css("meta[property='og:title']")),
            "",
        )
        title, title_source = pick_first(
            ("next_data", item_detail.get("title", "")),
            ("meta", meta_title),
        )
        result["title"] = title
        if title_source:
            field_sources["title"] = title_source
        page_text = _get_page_text(page)

        result["price"] = _extract_tax_inclusive_price(item_detail, page_text)
        if result["price"] is not None:
            field_sources["price"] = "next_data"

        raw_description = item_detail.get("description", "") or item_detail.get("itemDescription", "")
        if isinstance(raw_description, list):
            raw_description = "\n".join(str(d) for d in raw_description)
        meta_description = ""
        meta_el = page.css("meta[name='description']")
        if meta_el:
            meta_description = str(meta_el[0].attrib.get("content", "") or "")
        description, description_source = pick_first(
            ("next_data", str(raw_description or "")),
            ("meta", meta_description),
        )
        if description:
            result["description"] = description
            field_sources["description"] = description_source

        image_urls = []
        for key in ("img", "images", "image", "imageList"):
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
                    field_sources["images"] = "meta"
        elif image_urls:
            field_sources["images"] = "next_data"
        result["image_urls"] = image_urls

        result["status"] = _infer_auction_status(item_detail, page_text)
        field_sources["status"] = "next_data" if item_detail else "css"

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
            field_sources["variants"] = "next_data"

        return attach_extraction_trace(result, strategy="next_data", field_sources=field_sources)
    except Exception as exc:
        logger.debug("Yahuoku light scrape error: %s", exc)
        return {}


def scrape_item_detail(url_or_driver, maybe_url=None, **_kwargs) -> dict:
    """
    Yahoo Auctions detail scrape.
    The legacy `(driver, url)` signature is accepted for backward compatibility.
    """
    url = _resolve_detail_url(url_or_driver, maybe_url)
    result = scrape_item_detail_light(url) or _empty_result(url)
    report_detail_result("yahuoku", url, result, result.get("_scrape_meta"), page_type="detail")
    return result


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
