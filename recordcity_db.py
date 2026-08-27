"""
Record City (recordcity.jp) scraping module.

The site publishes schema.org ``Product`` JSON-LD on every catalog page, so the
detail reader takes its facts from there rather than from the markup: name,
images, catalogue number, brand, price, availability and whether the record is
new or used all arrive already labelled.

Both kinds of page sit behind an AWS WAF challenge that answers a plain HTTP
request with a JavaScript puzzle instead of the page, so every fetch here goes
through the browser path. A static fetch returns the challenge and nothing
useful.

Listing pages carry no JSON-LD, so a search collects catalog links from the
listing and reads each product page in turn. Some categories hold six figures
of records, which is why the caller's item count is a hard ceiling on both the
links collected and the pages opened.
"""
import json
import logging
import re
from urllib.parse import urljoin

from services.scrape_safety import (
    ScrapeFailure,
    UnsafeScrapeUrlError,
    is_usable_detail_result,
    raise_for_unsafe_detail_result,
    require_search_outcome,
    require_usable_details,
    validate_fetch_response,
    validate_marketplace_url,
)

logger = logging.getLogger(__name__)

SITE = "recordcity"

#: schema.org availability values, mapped to the app's own status vocabulary.
_AVAILABILITY_STATUS = {
    "instock": "on_sale",
    "limitedavailability": "on_sale",
    "preorder": "on_sale",
    "backorder": "on_sale",
    "outofstock": "sold_out",
    "soldout": "sold_out",
    "discontinued": "sold_out",
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
        "sku": "",
    }


def _page_text(page) -> str:
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


def _iter_json_ld(page, broken=None):
    """
    Yield every JSON-LD object on the page, unwrapping lists and @graph.

    ``broken`` collects the parse errors. Structured data that is present but
    malformed looks identical to none at all from the caller's side, and the
    two want different answers — one is the site's markup, the other is
    usually the bot challenge still on screen.
    """
    for script_el in page.css("script[type='application/ld+json']"):
        raw = str(getattr(script_el, "text", "") or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            if broken is not None:
                broken.append(str(exc))
            continue
        pending = data if isinstance(data, list) else [data]
        while pending:
            entry = pending.pop(0)
            if not isinstance(entry, dict):
                continue
            graph = entry.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)
            yield entry


def _extract_json_ld_product(page, broken=None) -> dict:
    for entry in _iter_json_ld(page, broken):
        entry_type = entry.get("@type")
        types = entry_type if isinstance(entry_type, list) else [entry_type]
        if any(str(value).lower() == "product" for value in types if value):
            return entry
    return {}


def _first_offer(product: dict) -> dict:
    offers = product.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                return offer
    return {}


def _parse_price(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return int(float(match.group(0)))
    except ValueError:
        return None


def _schema_tail(value) -> str:
    """Turn "https://schema.org/InStock" into "InStock"."""
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name") or ""
    return re.sub(r"^https?://schema\.org/", "", str(value or "")).strip()


def _infer_status(offer: dict) -> str:
    availability = _schema_tail(offer.get("availability")).lower()
    return _AVAILABILITY_STATUS.get(availability, "unknown")


def _extract_images(product: dict) -> list:
    images = product.get("image")
    if isinstance(images, str):
        candidates = [images]
    elif isinstance(images, list):
        candidates = images
    else:
        candidates = []

    urls = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("url") or candidate.get("contentUrl") or ""
        candidate = str(candidate or "").strip()
        if candidate.startswith("http") and candidate not in urls:
            urls.append(candidate)
    return urls


def _brand_name(product: dict) -> str:
    brand = product.get("brand")
    if isinstance(brand, dict):
        return str(brand.get("name") or "").strip()
    return str(brand or "").strip()


#: What the real page has and the challenge page does not. Waiting on it is how
#: we tell "the puzzle is still running" from "the markup is here".
_READY_SELECTOR = {
    "detail": "script[type='application/ld+json']",
    "search": "a[href*='/catalog/']",
}

#: The challenge runs, then reloads. Five seconds — the shared default — is not
#: enough for both, and the page that comes back too early carries no product.
_READY_TIMEOUT_MS = 20000


def _fetch_page(url: str, kind: str):
    """Fetch through the browser; the WAF answers anything else with a puzzle."""
    from services.scraping_client import fetch_dynamic

    page = fetch_dynamic(
        url,
        network_idle=True,
        timeout=45000,
        wait_selector=_READY_SELECTOR[kind],
        wait_selector_timeout=_READY_TIMEOUT_MS,
    )
    validate_fetch_response(page, SITE, kind=kind)
    return page


def scrape_item_detail(url_or_driver=None, maybe_url=None, **_kwargs) -> dict:
    """
    Read one Record City product page.

    The ``driver`` first argument exists because the dispatcher calls every
    site module the same way; it is not used.
    """
    url = maybe_url if isinstance(maybe_url, str) and maybe_url else url_or_driver
    if not isinstance(url, str) or not url:
        raise ValueError("url is required")

    try:
        url = validate_marketplace_url(url, SITE, kind="detail")
        page = _fetch_page(url, kind="detail")
    except ScrapeFailure:
        raise
    except Exception as exc:
        # Saying only "could not be read" leaves nobody able to act. The cause
        # travels with the failure so the operator, and the log, name it.
        logger.warning("Record City detail fetch failed for %s: %s", url, exc)
        raise ScrapeFailure(
            f"レコードシティの商品ページを読み取れませんでした: {exc}"
        ) from exc

    broken_blocks = []
    product = _extract_json_ld_product(page, broken_blocks)
    if not product:
        if broken_blocks:
            # The data is there and unreadable, which is a different problem
            # from it not being there — and saying "bot challenge" here would
            # send everyone looking in the wrong place.
            logger.warning(
                "Record City structured data could not be parsed (%d block(s)): %s | %s",
                len(broken_blocks),
                url,
                broken_blocks[0],
            )
            raise ScrapeFailure(
                f"レコードシティのページの構造化データを読み取れませんでした"
                f"（{len(broken_blocks)}件が解析エラー）: {broken_blocks[0]}"
            )
        # Nothing there at all: usually the bot challenge still on screen.
        logger.warning("Record City page carried no product data: %s", url)
        raise ScrapeFailure(
            "レコードシティのページから商品データが見つかりませんでした。"
            "サイト側のボット判定が解けていないか、ページ構成が変わった可能性があります。"
        )

    result = _empty_result(url, status="unknown")
    offer = _first_offer(product)

    result["title"] = str(product.get("name") or "").strip()
    result["brand"] = _brand_name(product)
    result["description"] = str(product.get("description") or "").strip()
    result["image_urls"] = _extract_images(product)
    result["sku"] = str(product.get("sku") or "").strip()
    # schema.org allows the condition on either the product or the offer, and
    # this site puts it on the offer.
    result["condition"] = _schema_tail(
        offer.get("itemCondition") or product.get("itemCondition")
    )
    result["status"] = _infer_status(offer)

    currency = str(offer.get("priceCurrency") or "").upper()
    if currency and currency != "JPY":
        # Prices are stored as yen throughout the app, so a foreign amount
        # would be wrong rather than merely missing.
        logger.warning("Record City offer is in %s, not JPY: %s", currency, url)
    else:
        result["price"] = _parse_price(offer.get("price"))

    return result


def scrape_single_item(url: str, headless: bool = True) -> list:
    """Dispatcher entry point for a single pasted product URL."""
    result = scrape_item_detail(url)
    return [result] if is_usable_detail_result(result) else []


def _extract_search_urls(page, base_url: str, max_items: int) -> list:
    urls = []
    seen = set()
    for anchor in page.css("a[href]"):
        href = str(anchor.attrib.get("href", "") or "").strip()
        # A cheap first pass; the validator below is what actually decides,
        # and it is the one that keeps the crawl on this site.
        if not href or "/catalog/" not in href:
            continue
        full_url = urljoin(base_url, href)
        try:
            full_url = validate_marketplace_url(full_url, SITE, kind="detail")
        except UnsafeScrapeUrlError:
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        urls.append(full_url)
        if len(urls) >= max_items:
            break
    return urls


def _find_next_page_url(page, current_url: str) -> str:
    for anchor in page.css("a[href]"):
        href = str(anchor.attrib.get("href", "") or "").strip()
        if not href:
            continue
        label = str(getattr(anchor, "text", "") or "").strip()
        rel = str(anchor.attrib.get("rel", "") or "").lower()
        classes = str(anchor.attrib.get("class", "") or "").lower()
        if "次へ" in label or "next" in rel or "next" in classes:
            try:
                return validate_marketplace_url(
                    urljoin(current_url, href), SITE, kind="search"
                )
            except UnsafeScrapeUrlError:
                continue
    return ""


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
) -> list:
    """
    Read a Record City listing page and then each product it links to.

    ``max_items`` bounds both halves of the work. A category can hold six
    figures of records, so collecting every link before filtering would be a
    long crawl for a request that wanted ten.
    """
    requested = max(1, int(max_items or 1))
    # A little headroom, because some links will turn out unreadable.
    candidate_target = min(requested * 2, requested + 40)

    results = []
    candidate_urls = []
    first_page_text = ""

    search_url = validate_marketplace_url(search_url, SITE, kind="search")
    current_url = search_url
    seen_pages = set()
    max_pages = max(1, int(max_scroll or 1))

    while current_url and current_url not in seen_pages and len(seen_pages) < max_pages:
        seen_pages.add(current_url)
        page = _fetch_page(current_url, kind="search")
        if not first_page_text:
            first_page_text = _page_text(page)

        for item_url in _extract_search_urls(page, current_url, candidate_target):
            if item_url not in candidate_urls:
                candidate_urls.append(item_url)
            if len(candidate_urls) >= candidate_target:
                break
        if len(candidate_urls) >= candidate_target:
            break
        current_url = _find_next_page_url(page, current_url)

    require_search_outcome(
        SITE, candidate_count=len(candidate_urls), text=first_page_text
    )

    for item_url in candidate_urls:
        if len(results) >= requested:
            break
        try:
            result = scrape_item_detail(item_url)
        except Exception as exc:
            raise_for_unsafe_detail_result(SITE, exc)
            logger.warning("Record City detail scrape failed for %s: %s", item_url, exc)
            continue
        raise_for_unsafe_detail_result(SITE, result)
        if is_usable_detail_result(result):
            results.append(result)

    require_usable_details(
        SITE, candidate_count=len(candidate_urls), item_count=len(results)
    )
    return results
