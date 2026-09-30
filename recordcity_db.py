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
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from services.scrape_safety import (
    ScrapeBlockedError,
    ScrapeFailure,
    ScrapeHttpError,
    ScrapeSelectorDriftError,
    SearchResult,
    UnsafeScrapeUrlError,
    is_usable_detail_result,
    raise_for_unsafe_detail_result,
    require_search_outcome,
    require_usable_details,
    has_no_results_evidence,
    validate_fetch_response,
    validate_marketplace_url,
)
from services.search_result_quality import search_item_identity
from services.listing_cards import validate_listing_card

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
_MAX_JSON_LD_SCRIPTS = 32
_MAX_JSON_LD_SCRIPT_BYTES = 1024 * 1024
_MAX_JSON_LD_NODES = 256


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
    for script_index, script_el in enumerate(
        page.css("script[type='application/ld+json']")
    ):
        if script_index >= _MAX_JSON_LD_SCRIPTS:
            if broken is not None:
                broken.append("JSON-LD script count exceeded safety limit")
            break
        raw = str(
            getattr(script_el, "raw_text", "")
            or getattr(script_el, "text", "")
            or ""
        ).strip()
        if not raw:
            continue
        if len(raw.encode("utf-8", errors="ignore")) > _MAX_JSON_LD_SCRIPT_BYTES:
            if broken is not None:
                broken.append("JSON-LD script exceeded safety limit")
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            if broken is not None:
                broken.append(str(exc))
            continue
        pending = data if isinstance(data, list) else [data]
        processed_nodes = 0
        while pending and processed_nodes < _MAX_JSON_LD_NODES:
            entry = pending.pop(0)
            processed_nodes += 1
            if not isinstance(entry, dict):
                continue
            graph = entry.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)
            yield entry


def _extract_json_ld_product(
    page,
    broken=None,
    *,
    expected_sku: str | None = None,
    observed_product_skus: list[str] | None = None,
) -> dict:
    """Return the first Product, or the Product matching ``expected_sku``.

    Detail pages can contain more than one Product node (for example, stale
    structured data left in the DOM during a challenge reload).  Callers that
    know the requested catalog ID must select by SKU rather than trusting the
    first Product node on the page.

    ``observed_product_skus`` lets the caller distinguish "no Product data"
    from "Product data was present, but it belonged to another catalog ID"
    without parsing the bounded JSON-LD collection a second time.
    """
    normalized_expected = (
        str(expected_sku).strip() if expected_sku is not None else None
    )
    for entry in _iter_json_ld(page, broken):
        entry_type = entry.get("@type")
        types = entry_type if isinstance(entry_type, list) else [entry_type]
        if any(str(value).lower() == "product" for value in types if value):
            sku = str(entry.get("sku") or "").strip()
            if observed_product_skus is not None:
                observed_product_skus.append(sku)
            if normalized_expected is None or sku == normalized_expected:
                return entry
    return {}


def _catalog_id(url: str) -> str:
    """Extract the exact numeric catalog ID from a validated detail URL."""
    try:
        path = str(urlparse(str(url or "")).path or "")
    except ValueError:
        return ""
    match = re.fullmatch(r"/(?:[a-z]{2}/)?catalog/(\d+)/?", path)
    return str(match.group(1) or "") if match else ""


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
    """Fetch through the RecordCity-only guarded provider orchestration."""
    from services.recordcity_external_fetch import fetch_recordcity_page_sync

    page = fetch_recordcity_page_sync(
        url,
        kind=kind,
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

    expected_sku = _catalog_id(url)
    observed_product_skus = []
    broken_blocks = []
    product = _extract_json_ld_product(
        page,
        broken_blocks,
        expected_sku=expected_sku,
        observed_product_skus=observed_product_skus,
    )
    if not product:
        if observed_product_skus:
            # Returning a different Product is silent data corruption: title,
            # price and inventory would all be registered against the wrong
            # catalog URL. Keep the reason stable for logs and the UI.
            logger.warning(
                "Record City product identity mismatch (expected=%s, product_nodes=%d): %s",
                expected_sku,
                len(observed_product_skus),
                url,
            )
            raise ScrapeFailure(
                "レコードシティの商品ページに要求した商品IDと一致するデータが"
                "見つかりませんでした（reason=RC_DETAIL_IDENTITY_MISMATCH）。"
            )
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
        identity = search_item_identity({"url": full_url}, site=SITE)
        if identity in seen:
            continue
        seen.add(identity)
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


def _scrape_search_result_in_navigation_session(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
    progress_callback=None,
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
    processed_count = 0
    detail_error_count = 0
    candidate_identities = set()
    end_reason = "unknown"

    def checkpoint(phase):
        if progress_callback is not None:
            return progress_callback(list(results), {
                "phase": phase,
                "pages_fetched": len(seen_pages),
                "candidates_count": len(candidate_urls),
                "processed_count": processed_count,
                "detail_error_count": detail_error_count,
                "end_reason": end_reason,
            })

    while current_url and current_url not in seen_pages and len(seen_pages) < max_pages:
        seen_pages.add(current_url)
        page = _fetch_page(current_url, kind="search")
        if not first_page_text:
            first_page_text = _page_text(page)

        for item_url in _extract_search_urls(page, current_url, candidate_target):
            identity = search_item_identity({"url": item_url}, site=SITE)
            if identity not in candidate_identities:
                candidate_identities.add(identity)
                candidate_urls.append(item_url)
            if len(candidate_urls) >= candidate_target:
                break
        checkpoint("listing")
        if len(candidate_urls) >= candidate_target:
            end_reason = "candidate_limit"
            break
        current_url = _find_next_page_url(page, current_url)

    if end_reason != "candidate_limit":
        if current_url in seen_pages:
            end_reason = "pagination_loop"
        elif current_url and len(seen_pages) >= max_pages:
            end_reason = "page_limit"
        elif not candidate_urls and has_no_results_evidence(first_page_text, SITE):
            end_reason = "explicit_empty"
        # Missing a next link does not establish normal listing exhaustion.

    require_search_outcome(
        SITE, candidate_count=len(candidate_urls), text=first_page_text
    )

    for item_url in candidate_urls:
        if len(results) >= requested:
            break
        processed_count += 1
        try:
            result = scrape_item_detail(item_url)
        except ScrapeBlockedError:
            # A CAPTCHA/rate block applies to the browser session or egress,
            # not just one catalog item. Continuing through every candidate
            # would create a burst of futile requests and hide the actionable
            # WAF reason from the operator.
            raise
        except ScrapeHttpError as exc:
            if exc.status_code == 429:
                # Some external providers expose a target-side 429 as an
                # HTTP failure rather than ScrapeBlockedError. It is still a
                # job-wide rate signal and must not fan out over candidates.
                raise
            logger.warning("Record City detail scrape failed for %s: %s", item_url, exc)
            detail_error_count += 1
            checkpoint("details")
            continue
        except Exception as exc:
            raise_for_unsafe_detail_result(SITE, exc)
            logger.warning("Record City detail scrape failed for %s: %s", item_url, exc)
            detail_error_count += 1
            checkpoint("details")
            continue
        raise_for_unsafe_detail_result(SITE, result)
        if is_usable_detail_result(result):
            results.append(result)
        else:
            detail_error_count += 1
        # The job knows the user's filters and desired count. Its explicit
        # completion signal avoids spending the remaining budget on internal
        # overfetch after enough verified, publishable items are available.
        if checkpoint("details") is True:
            end_reason = "requested_reached"
            break

    require_usable_details(
        SITE, candidate_count=len(candidate_urls), item_count=len(results)
    )
    checkpoint("completed")
    return results


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
    progress_callback=None,
) -> list:
    """Read one listing and its products in a job-scoped browser session."""
    from services.recordcity_browser_fetch import recordcity_navigation_session

    with recordcity_navigation_session():
        return _scrape_search_result_in_navigation_session(
            search_url,
            max_items=max_items,
            max_scroll=max_scroll,
            headless=headless,
            progress_callback=progress_callback,
        )


# This shallow adapter intentionally recognizes only explicit schema.org
# ItemList/Product data. No RecordCity-specific card DOM has been verified.
# Keep its application feature gate disabled until real listing fixtures prove
# compatibility; do not fall back to fetching every product's detail page.
LISTING_MAX_ITEMS = 500
LISTING_MAX_PAGES = 10
LISTING_MAX_SECONDS = 180


def _schema_type(node: dict, expected: str) -> bool:
    values = node.get("@type", [])
    if not isinstance(values, list):
        values = [values]
    return any(_schema_tail(value).lower() == expected.lower() for value in values)


def _listing_price(value):
    """Read an exact positive JPY amount, never numbers from arbitrary prose."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    text = str(value).strip()
    if re.fullmatch(r"(?:[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)(?:\.0+)?", text) is None:
        return None
    try:
        amount = Decimal(text.replace(",", ""))
        return int(amount) if amount.is_finite() and amount > 0 else None
    except (ValueError, InvalidOperation, OverflowError):
        return None


def _listing_card_from_product(product: dict, base_url: str) -> dict | None:
    if not _schema_type(product, "Product"):
        return None
    raw_url = product.get("url") or product.get("@id")
    if not isinstance(raw_url, str):
        return None
    try:
        url = validate_marketplace_url(urljoin(base_url, raw_url), SITE, kind="detail")
    except (UnsafeScrapeUrlError, ValueError):
        return None
    source_id = _catalog_id(url)
    sku = product.get("sku")
    if sku is not None and str(sku) != source_id:
        return None
    offers = product.get("offers")
    if isinstance(offers, list) and len(offers) == 1:
        offers = offers[0]
    if not isinstance(offers, dict) or offers.get("priceCurrency") != "JPY":
        return None
    if offers.get("@type") and not _schema_type(offers, "Offer"):
        return None
    images = _extract_images(product)
    if not images:
        return None
    status = _infer_status(offers)
    item = {
        "_listing_card": True,
        "url": url,
        "source_id": source_id,
        "title": product.get("name"),
        "price": _listing_price(offers.get("price")),
        "currency": "JPY",
        "status": "sold" if status == "sold_out" else status,
        "description": "",
        "image_urls": images[:1],
    }
    return item if validate_listing_card(item, SITE) else None


def _extract_listing_cards(page, base_url: str) -> tuple[list[dict], int]:
    """Return bounded explicit list cards and the number of rejected entries.

    A standalone Product or breadcrumb ItemList is not evidence of a product
    listing. ItemList entries must identify Product nodes in their own scope.
    """
    cards = []
    invalid_count = 0
    inspected = 0
    for node in _iter_json_ld(page):
        if not _schema_type(node, "ItemList"):
            continue
        entries = node.get("itemListElement")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            inspected += 1
            if inspected > LISTING_MAX_ITEMS * 2:
                return cards, invalid_count + 1
            if not isinstance(entry, dict):
                invalid_count += 1
                continue
            product = entry.get("item") if _schema_type(entry, "ListItem") else entry
            if not isinstance(product, dict) or not _schema_type(product, "Product"):
                # Breadcrumbs or links alone cannot become display cards.
                continue
            item = _listing_card_from_product(product, base_url)
            if item is None:
                invalid_count += 1
            else:
                cards.append(item)
    return cards, invalid_count


def _listing_page_identity(url: str) -> tuple:
    parsed = urlparse(url)
    return (parsed.path.rstrip("/"), tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True))))


def _listing_has_empty_evidence(page) -> bool:
    # The shared legacy marker "0件" also matches "100件". A shallow adapter
    # must not turn unsupported card markup into a verified empty result.
    text = re.sub(r"\s+", " ", _page_text(page)).strip()
    phrases = ("該当する商品がありません", "該当の商品がありません",
               "検索結果がありません", "商品が見つかりませんでした")
    empty = any(phrase in text for phrase in phrases) or bool(
        re.search(r"(?:検索結果|該当商品|該当する商品|商品数)\s*(?:[:：=はが]\s*)?0\s*件", text)
    )
    if not empty:
        return False
    # A product link contradicts a zero-result assertion when its card shape
    # was not understood. Retain uncertainty instead of silently hiding it.
    return not any("/catalog/" in str(anchor.attrib.get("href", ""))
                   for anchor in page.css("a[href]"))


def _listing_next_url(page, current_url: str, original_url: str) -> str:
    """Follow an explicit next link without dropping or changing filters."""
    target = _find_next_page_url(page, current_url)
    if not target:
        return ""
    original = urlparse(original_url)
    parsed = urlparse(target)
    if parsed.path.rstrip("/") != original.path.rstrip("/"):
        raise ScrapeSelectorDriftError("RecordCityのページ送り先が検索条件と一致しません。")
    original_pairs = parse_qsl(original.query, keep_blank_values=True)
    next_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filters = [(key, value) for key, value in original_pairs if key != "page"]
    next_filters = [(key, value) for key, value in next_pairs if key != "page"]
    original_by_key = {}
    for key, value in filters:
        original_by_key.setdefault(key, []).append(value)
    next_by_key = {}
    for key, value in next_filters:
        next_by_key.setdefault(key, []).append(value)
    if any(key not in original_by_key or values != original_by_key[key]
           for key, values in next_by_key.items()):
        raise ScrapeSelectorDriftError("RecordCityのページ送りで検索条件の変更を検出しました。")
    page_values = [value for key, value in next_pairs if key == "page"]
    if len(page_values) != 1 or not re.fullmatch(r"[1-9]\d*", page_values[0]):
        raise ScrapeSelectorDriftError("RecordCityのページ送り番号を確認できませんでした。")
    query = urlencode(filters + [("page", page_values[0])])
    return validate_marketplace_url(urlunparse(parsed._replace(query=query, fragment="")), SITE, kind="search")


def scrape_listing_result(
    search_url: str,
    max_items: int = 100,
    max_pages: int = 6,
    progress_callback=None,
) -> list:
    """Collect shallow cards with zero detail fetches and bounded page work.

    The 180-second budget is checked before each page request. An in-flight
    fetch retains the existing finite transport timeout. At most ten page
    fetch calls occur, regardless of untrusted next links or caller limits.
    Missing next links and unsupported markup never prove list exhaustion.
    """
    from services.recordcity_browser_fetch import recordcity_navigation_session

    search_url = validate_marketplace_url(search_url, SITE, kind="search")
    limit = min(LISTING_MAX_ITEMS, max(1, int(max_items)))
    page_limit = min(LISTING_MAX_PAGES, max(1, int(max_pages)))
    results, identities, seen_pages = [], set(), set()
    duplicate_count = invalid_count = candidate_count = 0
    pages_fetched = 0
    end_reason = "unknown"
    current_url = search_url
    started = time.monotonic()

    def checkpoint(phase):
        if progress_callback is not None:
            progress_callback(list(results), {
                "phase": phase,
                "pages_fetched": pages_fetched,
                "candidates_count": candidate_count,
                "processed_count": candidate_count,
                "detail_error_count": 0,
                "invalid_card_count": invalid_count,
                "duplicate_count": duplicate_count,
                "end_reason": end_reason,
            })

    with recordcity_navigation_session():
        while current_url:
            if time.monotonic() - started >= LISTING_MAX_SECONDS:
                checkpoint("listing")
                raise ScrapeFailure("RecordCityの一覧取得が制限時間に達しました。取得済み商品を確認してください。")
            identity = _listing_page_identity(current_url)
            if identity in seen_pages:
                end_reason = "pagination_loop"
                break
            if pages_fetched >= page_limit:
                end_reason = "page_limit"
                break
            page = _fetch_page(current_url, kind="search")
            final_url = getattr(page, "url", "")
            if final_url:
                validate_marketplace_url(final_url, SITE, kind="search")
                if _listing_page_identity(final_url) != identity:
                    raise ScrapeSelectorDriftError("RecordCityの応答で検索条件の変更を検出しました。")
            seen_pages.add(identity)
            pages_fetched += 1
            cards, invalid = _extract_listing_cards(page, current_url)
            invalid_count += invalid
            candidate_count += len(cards) + invalid
            if not cards and invalid:
                checkpoint("listing")
                raise ScrapeSelectorDriftError("RecordCityの一覧商品に必要な表示情報を確認できませんでした。")
            page_reason = require_search_outcome(
                SITE, candidate_count=len(cards), text=_page_text(page)
            )
            if page_reason == "explicit_empty":
                if not _listing_has_empty_evidence(page):
                    raise ScrapeSelectorDriftError("RecordCityの一覧が0件という根拠を確認できませんでした。")
                if not results and not invalid_count:
                    end_reason = "explicit_empty"
                    break
            for item in cards:
                item_identity = search_item_identity(item, site=SITE)
                if item_identity in identities:
                    duplicate_count += 1
                    continue
                identities.add(item_identity)
                results.append(item)
                if len(results) >= limit:
                    end_reason = "requested_reached"
                    break
            checkpoint("listing")
            if end_reason == "requested_reached":
                break
            current_url = _listing_next_url(page, current_url, search_url)
        checkpoint("completed")
    return SearchResult(results, end_reason=end_reason)
