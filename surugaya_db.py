"""
Surugaya scraper - Product detail scraping for suruga-ya.jp
Uses curl_cffi to bypass Cloudflare protection by impersonating Chrome's TLS fingerprint.
"""
import html
import json
import logging
import os
import re
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import requests

logger = logging.getLogger("surugaya")

BASE_URL = "https://www.suruga-ya.jp"
BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "window._cf_chl_opt",
    "cf-challenge-running",
    "challenge-form",
)

# CSS Selectors
SELECTORS = {
    "title": [
        "h1",
        ".product_title h1",
        "[itemprop='name']",
    ],
    "price": [
        ".price_group .text-price-detail",
        ".price_group label",
        "span.text-price-detail",
        "[itemprop='price']",
    ],
    "stock_available": [
        ".btn_buy",
        ".cart1",
        "#cart-add",
        "button[class*='cart']",
    ],
    "stock_sold": [
        ".waitbtn",
        ".soldout",
        ".outofstock",
    ],
    "main_image": [
        ".is-main-image img",
        "#item_picture",
        "img.main-pro-img",
        "#image_default",
        "img[src*='cdn.suruga-ya.jp/database/']",
        "img[data-src*='cdn.suruga-ya.jp/database/']",
    ],
    "description": [
        ".tbl_product_info",
        "#item_condition",
        "#product_detail",
        "[itemprop='description']",
    ],
    "condition": [
        ".price_group label",
        ".condition",
        ".item_state",
    ],
    "category": [
        ".breadcrumb a",
        ".left div a[href*='category=']",
    ],
    "product_links": [
        ".item a[href*='/product/detail/']",
        "a[href*='/product/detail/']",
    ],
    "pagination_links": [
        "a[href*='page=']",
    ],
}


def get_session():
    """Create a curl_cffi session that impersonates Chrome."""
    session = requests.Session(impersonate="chrome120")
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": BASE_URL + "/",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    })
    return session


def _is_cloudflare_block(resp) -> bool:
    if resp is None:
        return False
    text = (resp.text or "").lower()
    return resp.status_code in (403, 429, 503) or any(marker in text for marker in BLOCK_MARKERS)


def _normalize_url(raw_url: str, base_url: str) -> str:
    if not raw_url:
        return ""
    url = raw_url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_url, url)


def _dedupe_keep_order(values):
    seen = set()
    out = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _extract_price(text: str):
    if not text:
        return None

    cleaned = text.replace("，", ",").replace("\u3000", " ")
    patterns = [
        r"([0-9][0-9,]{1,})\s*円",
        r"[¥￥]\s*([0-9][0-9,]{1,})",
        r"税込\s*([0-9][0-9,]{1,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        try:
            return int(match.group(1).replace(",", ""))
        except Exception:
            continue
    return None


def _extract_price_from_body(text: str):
    if not text:
        return None

    patterns = [
        r"([0-9][0-9,]{1,})\s*円\s*\(税込\)",
        r"(?:販売価格|価格|税込)[^\d]{0,8}([0-9][0-9,]{1,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return int(match.group(1).replace(",", ""))
        except Exception:
            continue
    return None


def _fetch_with_retry(session, url: str, timeout: int = 30, max_attempts: int = 3):
    last_response = None
    last_error = None

    for attempt in range(max_attempts):
        try:
            response = session.get(url, timeout=timeout)
            last_response = response
            last_error = None
        except Exception as exc:
            response = None
            last_error = exc

        if response is not None and not _is_cloudflare_block(response) and response.status_code < 500:
            return response, None

        if attempt < max_attempts - 1:
            try:
                session.get(BASE_URL + "/", timeout=10)
            except Exception:
                pass
            time.sleep(1.0 + (attempt * 0.8))

    if last_response is not None:
        return last_response, None
    return None, last_error


def _extract_json_ld_product(soup: BeautifulSoup) -> dict:
    result = {}
    scripts = soup.select("script[type='application/ld+json']")

    for script in scripts:
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw.strip())
        except Exception:
            continue

        nodes = []
        if isinstance(data, list):
            nodes = data
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes = data["@graph"]
        elif isinstance(data, dict):
            nodes = [data]

        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type")
            if isinstance(node_type, list):
                types = [str(t).lower() for t in node_type]
            else:
                types = [str(node_type).lower()]
            if "product" not in types:
                continue

            if not result.get("name") and node.get("name"):
                result["name"] = str(node["name"]).strip()

            if not result.get("images"):
                image_data = node.get("image")
                if isinstance(image_data, list):
                    result["images"] = [str(i) for i in image_data if i]
                elif isinstance(image_data, str):
                    result["images"] = [image_data]

            offers = node.get("offers")
            offer_obj = None
            if isinstance(offers, list) and offers:
                offer_obj = offers[0]
            elif isinstance(offers, dict):
                offer_obj = offers

            if isinstance(offer_obj, dict):
                if result.get("price") is None:
                    price_raw = offer_obj.get("price")
                    if price_raw is not None:
                        parsed = _extract_price(str(price_raw))
                        if parsed is None:
                            try:
                                parsed = int(float(str(price_raw).replace(",", "")))
                            except Exception:
                                parsed = None
                        result["price"] = parsed
                if not result.get("availability"):
                    availability = offer_obj.get("availability")
                    if availability:
                        result["availability"] = str(availability).lower()

    return result


def _is_placeholder_image(url: str) -> bool:
    lowered = url.lower()
    return (
        "no_photo" in lowered
        or "bglogo" in lowered
        or "logo-surugaya" in lowered
    )


def _extract_image_urls(soup: BeautifulSoup, page_url: str, ld_product: dict) -> list:
    image_urls = []

    for selector in SELECTORS["main_image"]:
        for img in soup.select(selector):
            raw = img.get("src") or img.get("data-src") or img.get("data-original")
            if not raw:
                srcset = img.get("srcset")
                if srcset:
                    raw = srcset.split(",")[0].strip().split(" ")[0]
            if not raw:
                continue

            full_url = _normalize_url(raw, page_url)
            if not full_url or _is_placeholder_image(full_url):
                continue
            image_urls.append(full_url)

    if not image_urls:
        for raw in ld_product.get("images", []) or []:
            full_url = _normalize_url(raw, page_url)
            if not full_url or _is_placeholder_image(full_url):
                continue
            image_urls.append(full_url)

    if not image_urls:
        og_image = soup.select_one("meta[property='og:image']")
        if og_image and og_image.get("content"):
            full_url = _normalize_url(og_image.get("content"), page_url)
            if full_url and not _is_placeholder_image(full_url):
                image_urls.append(full_url)

    return _dedupe_keep_order(image_urls)


def _extract_status(soup: BeautifulSoup, ld_product: dict) -> str:
    for selector in SELECTORS["stock_available"]:
        if soup.select(selector):
            return "active"

    for selector in SELECTORS["stock_sold"]:
        if soup.select(selector):
            return "sold"

    text = soup.get_text(" ", strip=True)
    sold_keywords = ("売り切れ", "在庫なし", "品切れ", "販売終了")
    active_keywords = ("カートに入れる", "購入手続き", "注文する")

    if any(keyword in text for keyword in sold_keywords):
        return "sold"
    if any(keyword in text for keyword in active_keywords):
        return "active"

    availability = ld_product.get("availability") or ""
    if "outofstock" in availability or "soldout" in availability:
        return "sold"
    if "instock" in availability:
        return "active"

    return "active"


def _extract_condition(soup: BeautifulSoup) -> str:
    for selector in SELECTORS["condition"]:
        for element in soup.select(selector):
            text = element.get_text(strip=True)
            if "中古" in text:
                return "中古"
            if "新品" in text:
                return "新品"
    return ""


def _extract_category(soup: BeautifulSoup) -> str:
    categories = []
    for selector in SELECTORS["category"]:
        for element in soup.select(selector):
            text = element.get_text(strip=True)
            if text:
                categories.append(text)
    categories = _dedupe_keep_order(categories)
    return " > ".join(categories)


def _extract_product_urls(soup: BeautifulSoup, base_url: str) -> list:
    product_urls = []
    for selector in SELECTORS["product_links"]:
        for anchor in soup.select(selector):
            href = anchor.get("href")
            if not href:
                continue
            full_url = _normalize_url(href, base_url)
            parsed = urlparse(full_url)
            if "/product/detail/" not in parsed.path:
                continue
            normalized = urlunparse(parsed._replace(fragment=""))
            product_urls.append(normalized)
    return _dedupe_keep_order(product_urls)


def _set_page_param(url: str, page_num: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _build_search_page_urls(search_url: str, first_soup: BeautifulSoup, max_scroll: int) -> list:
    max_pages = max(1, int(max_scroll or 1))
    page_urls = [search_url]

    if max_pages == 1:
        return page_urls

    discovered = []
    for selector in SELECTORS["pagination_links"]:
        for anchor in first_soup.select(selector):
            href = anchor.get("href")
            if not href:
                continue
            full_url = _normalize_url(href, search_url)
            match = re.search(r"[?&]page=(\d+)", full_url)
            if not match:
                continue
            discovered.append((int(match.group(1)), full_url))

    discovered.sort(key=lambda x: x[0])
    for page_num, page_url in discovered:
        if page_num <= 1:
            continue
        if page_url in page_urls:
            continue
        page_urls.append(page_url)
        if len(page_urls) >= max_pages:
            return page_urls

    # Fallback: synthesize page URLs when pagination links are missing.
    next_page = 2
    while len(page_urls) < max_pages:
        candidate = _set_page_param(search_url, next_page)
        if candidate not in page_urls:
            page_urls.append(candidate)
        next_page += 1

    return page_urls


def _should_use_selenium_fallback() -> bool:
    flag = os.getenv("SURUGAYA_SELENIUM_FALLBACK", "1").strip().lower()
    return flag not in ("0", "false", "off", "no")


def _should_use_yahoo_search_fallback() -> bool:
    flag = os.getenv("SURUGAYA_YAHOO_SEARCH_FALLBACK", "1").strip().lower()
    return flag not in ("0", "false", "off", "no")


def _should_use_global_domain_fallback() -> bool:
    flag = os.getenv("SURUGAYA_GLOBAL_DOMAIN_FALLBACK", "1").strip().lower()
    return flag not in ("0", "false", "off", "no")


def _extract_keyword_from_search_url(search_url: str) -> str:
    try:
        parsed = urlparse(search_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
    except Exception:
        return ""

    for key in ("search_word", "keyword", "q", "p"):
        values = query.get(key) or []
        for value in values:
            value = (value or "").strip()
            if value:
                return value
    return ""


def _looks_like_challenge_soup(soup: BeautifulSoup) -> bool:
    if soup is None:
        return False
    title_text = (soup.title.get_text(" ", strip=True).lower() if soup.title else "")
    if "just a moment" in title_text or "attention required" in title_text:
        return True

    html_text = str(soup).lower()
    markers = (
        "window._cf_chl_opt",
        "cf-challenge-running",
        "challenge-form",
        "/cdn-cgi/challenge-platform",
    )
    marker_hit = any(marker in html_text for marker in markers)
    if not marker_hit:
        return False

    # Avoid false positives on normal pages that include generic scripts.
    has_product_link = bool(soup.select("a[href*='/product/detail/']"))
    return not has_product_link


def _looks_like_challenge_html(title_text: str, html_text: str) -> bool:
    title_l = (title_text or "").lower()
    html_l = (html_text or "").lower()
    if "just a moment" in title_l or "attention required" in title_l:
        return True
    markers = (
        "window._cf_chl_opt",
        "cf-challenge-running",
        "challenge-form",
        "/cdn-cgi/challenge-platform",
    )
    return any(marker in html_l for marker in markers)


def _search_product_urls_via_yahoo(keyword: str, max_items: int) -> list:
    if not keyword:
        return []

    query = f"site:suruga-ya.jp/product/detail {keyword}"
    search_url = "https://search.yahoo.co.jp/search?p=" + quote_plus(query)
    urls = []

    try:
        session = requests.Session(impersonate="chrome120")
        session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Referer": "https://search.yahoo.co.jp/",
        })
        resp = session.get(search_url, timeout=20)
        if resp.status_code >= 400:
            logger.warning(f"Yahoo fallback search returned {resp.status_code}")
            return []

        soup = BeautifulSoup(resp.content, "html.parser")
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            if not href:
                continue

            # Yahoo may return direct links or redirect links that include target URL params.
            candidates = [href]
            parsed = urlparse(href)
            query_map = parse_qs(parsed.query, keep_blank_values=True)
            for key in ("RU", "ru", "url", "u"):
                for value in query_map.get(key, []):
                    if value:
                        candidates.append(unquote(html.unescape(value)))

            resolved = ""
            for candidate in candidates:
                candidate = candidate.strip()
                if "suruga-ya.jp/product/detail/" not in candidate:
                    continue
                resolved = _normalize_url(candidate, "https://search.yahoo.co.jp/")
                break

            if not resolved:
                continue
            urls.append(resolved)
            if len(urls) >= max_items:
                break

    except Exception as exc:
        logger.warning(f"Yahoo fallback search error: {exc}")
        return []

    return _dedupe_keep_order(urls)[:max_items]


def _build_global_product_url(jp_url: str) -> str:
    try:
        parsed = urlparse(jp_url)
        marker = "/product/detail/"
        if marker not in parsed.path:
            return ""
        code = parsed.path.split(marker, 1)[1].split("/")[0].strip()
        if not code:
            return ""
        return f"https://www.suruga-ya.com/ja/product/{code}"
    except Exception:
        return ""


def _extract_global_product_detail(source_url: str, global_url: str):
    session = get_session()
    resp, fetch_error = _fetch_with_retry(session, global_url, timeout=25, max_attempts=2)
    if fetch_error is not None:
        return None, fetch_error
    if resp is None:
        return None, "No response from global domain"
    if _is_cloudflare_block(resp):
        return None, f"Global domain blocked (status={resp.status_code})"
    if resp.status_code >= 400:
        return None, f"Global domain HTTP {resp.status_code}"

    soup = BeautifulSoup(resp.content, "html.parser")
    if _looks_like_challenge_soup(soup):
        return None, "Global domain challenge page"

    ld_product = _extract_json_ld_product(soup)

    title = ""
    title_el = soup.select_one("h1.title_product, h1")
    if title_el:
        title = title_el.get_text(" ", strip=True)
    if not title and ld_product.get("name"):
        title = ld_product["name"]
    if not title:
        og_title = soup.select_one("meta[property='og:title']")
        if og_title and og_title.get("content"):
            title = og_title.get("content").strip()

    # Variant-like price/stock blocks on global pages
    variants = []
    stock_values = []
    for option in soup.select("input[type='radio'][data-price]"):
        raw_price = option.get("data-price") or ""
        try:
            price = int(float(str(raw_price).replace(",", "")))
        except Exception:
            continue
        stock_raw = option.get("data-stock")
        try:
            stock = int(stock_raw) if stock_raw is not None and stock_raw != "" else 0
        except Exception:
            stock = 0
        name = (option.get("data-name") or "").strip()
        variant = {
            "option1_value": name or "Default Title",
            "price": price,
            "sku": "",
            "inventory_qty": stock if stock >= 0 else 0,
        }
        variants.append(variant)
        stock_values.append(stock)

    # Price selection: prefer positive prices to avoid placeholder 0 values.
    price = None
    in_stock_prices = [v["price"] for v in variants if v.get("inventory_qty", 0) > 0 and v.get("price", 0) > 0]
    if in_stock_prices:
        price = min(in_stock_prices)
    elif variants:
        positive_prices = [v["price"] for v in variants if v.get("price", 0) > 0]
        if positive_prices:
            price = min(positive_prices)
    elif ld_product.get("price") is not None:
        price = ld_product["price"]

    if price is not None and price <= 0:
        price = None

    # Status from variant stock / schema availability / sold keywords
    status = "unknown"
    if any(stock > 0 for stock in stock_values):
        status = "active"
    elif stock_values:
        status = "sold"
    else:
        availability = ld_product.get("availability") or ""
        if "instock" in availability:
            status = "active"
        elif "outofstock" in availability or "soldout" in availability:
            status = "sold"
        else:
            body_text = soup.get_text(" ", strip=True)
            if any(k in body_text for k in ("売り切れ", "在庫なし", "品切れ")):
                status = "sold"
            elif any(k in body_text for k in ("カートに入れる", "注文する")):
                status = "active"
            else:
                status = "active"

    condition = ""
    if variants:
        for v in variants:
            if v["price"] == price:
                name = v.get("option1_value", "")
                if "中古" in name:
                    condition = "中古"
                elif "新品" in name:
                    condition = "新品"
                break
    if not condition:
        cond_text = soup.get_text(" ", strip=True)
        if "中古" in cond_text:
            condition = "中古"
        elif "新品" in cond_text:
            condition = "新品"

    image_urls = _extract_image_urls(soup, resp.url or global_url, ld_product)
    if not image_urls:
        og_image = soup.select_one("meta[property='og:image']")
        if og_image and og_image.get("content"):
            og_url = _normalize_url(og_image.get("content"), resp.url or global_url)
            if og_url and not _is_placeholder_image(og_url):
                image_urls = [og_url]

    description = ""
    detail_el = soup.select_one("#product_detail_infor, .propertie_product, [itemprop='description']")
    if detail_el:
        description = detail_el.get_text("\n", strip=True)

    categories = []
    for el in soup.select("nav.breadcrumb a, .breadcrumb a"):
        t = el.get_text(strip=True)
        if t:
            categories.append(t)
    category = " > ".join(_dedupe_keep_order(categories))

    if not variants and price is not None:
        variants = [{
            "option1_value": condition or "Default Title",
            "price": price,
            "sku": "",
            "inventory_qty": 1 if status == "active" else 0,
        }]

    item = {
        "url": source_url,
        "title": title,
        "price": price,
        "status": status,
        "description": description,
        "image_urls": image_urls,
        "variants": variants,
        "condition": condition,
        "category": category,
    }
    return item, None


def _fetch_soup_with_selenium(url: str, headless: bool = True, wait_seconds: int = 20):
    """Fallback HTML fetch via Selenium (Render-safe fallback path)."""
    driver = None
    try:
        from mercari_db import create_driver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except Exception as exc:
        return None, url, str(exc)

    try:
        driver = create_driver(headless=headless)
        try:
            # Apply stealth script before first navigation when possible.
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
""".strip()
                },
            )
        except Exception:
            pass
        driver.get(url)
        WebDriverWait(driver, wait_seconds).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        challenge_retry_done = False
        deadline = time.time() + max(6, wait_seconds)
        while time.time() < deadline:
            html_text = driver.page_source or ""
            title_text = getattr(driver, "title", "") or ""
            current_url = getattr(driver, "current_url", url) or url
            if not html_text:
                time.sleep(0.8)
                continue

            if not _looks_like_challenge_html(title_text, html_text):
                soup = BeautifulSoup(html_text, "html.parser")
                return soup, current_url, None

            if not challenge_retry_done:
                challenge_retry_done = True
                try:
                    driver.refresh()
                except Exception:
                    pass
            time.sleep(1.2)

        # Return last HTML even if challenge remained, for caller-side handling/logging.
        html_text = driver.page_source or ""
        current_url = getattr(driver, "current_url", url) or url
        if not html_text:
            return None, current_url, "Empty page source"
        soup = BeautifulSoup(html_text, "html.parser")
        if _looks_like_challenge_soup(soup):
            return None, current_url, "Cloudflare challenge page remained after Selenium wait"
        return soup, current_url, None
    except Exception as exc:
        return None, url, str(exc)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def scrape_item_detail(session, url: str, headless: bool = True) -> dict:
    """
    駿河屋の商品ページから詳細情報を取得する (curl_cffi版)
    """
    result = {
        "url": url,
        "title": "",
        "price": None,
        "status": "unknown",
        "description": "",
        "image_urls": [],
        "variants": [],
        "condition": "",
        "category": "",
    }

    print(f"[SURUGAYA] Starting curl_cffi scrape for {url}")

    soup = None
    page_url = url
    resp, fetch_error = _fetch_with_retry(session, url, timeout=30, max_attempts=3)

    if fetch_error is None and resp is not None and not _is_cloudflare_block(resp):
        if resp.status_code >= 400:
            print(f"[SURUGAYA] WARN: HTTP status {resp.status_code} for {url}")
        try:
            soup = BeautifulSoup(resp.content, "html.parser")
            page_url = resp.url or url
        except Exception as exc:
            print(f"[SURUGAYA] ERROR during page load: {exc}")

    # Fallback for Render/Cloudflare 403: retry with real browser only for Surugaya.
    if soup is None and _should_use_selenium_fallback():
        status_code = resp.status_code if resp is not None else "N/A"
        print(f"[SURUGAYA] INFO: Trying Selenium fallback for item page (status={status_code})")
        selenium_soup, selenium_url, selenium_error = _fetch_soup_with_selenium(url, headless=headless)
        if selenium_soup is not None:
            soup = selenium_soup
            page_url = selenium_url or url
        elif selenium_error:
            print(f"[SURUGAYA] ERROR: Selenium fallback failed: {selenium_error}")

    if soup is None:
        if _should_use_global_domain_fallback():
            global_url = _build_global_product_url(url)
            if global_url:
                print(f"[SURUGAYA] INFO: Trying global domain fallback: {global_url}")
                global_item, global_error = _extract_global_product_detail(url, global_url)
                if global_item and global_item.get("title"):
                    logger.info(f"Scraped via global fallback: {global_item['title'][:30]}... - ¥{global_item.get('price')}")
                    return global_item
                if global_error:
                    print(f"[SURUGAYA] ERROR: Global fallback failed: {global_error}")

        if resp is not None and _is_cloudflare_block(resp):
            print(f"[SURUGAYA] ERROR: Cloudflare block detected (Status: {resp.status_code})")
            result["status"] = "blocked"
        elif fetch_error is not None:
            print(f"[SURUGAYA] ERROR during page load: {fetch_error}")
            result["status"] = "error"
        else:
            result["status"] = "error"
        return result

    ld_product = _extract_json_ld_product(soup)

    # ---- Title ----
    for selector in SELECTORS["title"]:
        title_el = soup.select_one(selector)
        if title_el:
            text = title_el.get_text(" ", strip=True)
            if text:
                result["title"] = text
                break
    if not result["title"] and ld_product.get("name"):
        result["title"] = ld_product["name"]

    # ---- Price ----
    for selector in SELECTORS["price"]:
        for el in soup.select(selector):
            price = _extract_price(el.get_text(" ", strip=True))
            if price is not None:
                result["price"] = price
                break
        if result["price"] is not None:
            break

    if result["price"] is None and ld_product.get("price") is not None:
        result["price"] = ld_product["price"]

    if result["price"] is None:
        result["price"] = _extract_price_from_body(soup.get_text(" ", strip=True))

    # ---- Stock ----
    result["status"] = _extract_status(soup, ld_product)

    # ---- Condition ----
    result["condition"] = _extract_condition(soup)

    # ---- Images ----
    result["image_urls"] = _extract_image_urls(soup, page_url, ld_product)

    # ---- Description ----
    for selector in SELECTORS["description"]:
        detail_el = soup.select_one(selector)
        if not detail_el:
            continue
        text = detail_el.get_text(separator="\n", strip=True)
        if text:
            result["description"] = text
            break

    # ---- Category ----
    result["category"] = _extract_category(soup)

    # Default variant for compatibility
    if result["price"] is not None:
        result["variants"] = [{
            "option1_value": result.get("condition") or "Default Title",
            "price": result["price"],
            "sku": "",
            "inventory_qty": 1 if result["status"] == "active" else 0
        }]
        
    logger.info(f"Scraped: {result['title'][:30]}... - ¥{result['price']} ({result['status']})")
    return result


def scrape_single_item(url: str, headless: bool = True) -> list:
    """
    指定された駿河屋商品URLを1件だけスクレイピングして list[dict] を返す。
    """
    try:
        session = get_session()
        result = scrape_item_detail(session, url, headless=headless)
        return [result] if result["title"] else []
    except Exception as e:
        print(f"[SURUGAYA] Error in scrape_single_item: {e}")
        return []


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
) -> list:
    """
    駿河屋検索結果から複数商品をスクレイピング (curl_cffi版)
    """
    results = []

    try:
        print(f"[SURUGAYA] Search: Initializing curl_cffi session...")
        session = get_session()

        print(f"[SURUGAYA] Search: Fetching {search_url}")
        first_resp, fetch_error = _fetch_with_retry(session, search_url, timeout=30, max_attempts=3)

        keyword = _extract_keyword_from_search_url(search_url)
        soup = None
        base_search_url = search_url
        product_urls = []
        if fetch_error is None and first_resp is not None and not _is_cloudflare_block(first_resp):
            soup = BeautifulSoup(first_resp.content, "html.parser")
            base_search_url = first_resp.url or search_url
        else:
            status_code = first_resp.status_code if first_resp is not None else "N/A"
            if _should_use_selenium_fallback():
                print(f"[SURUGAYA] Search: INFO: Trying Selenium fallback (status={status_code})")
                selenium_soup, selenium_url, selenium_error = _fetch_soup_with_selenium(
                    search_url,
                    headless=headless,
                )
                if selenium_soup is not None:
                    soup = selenium_soup
                    base_search_url = selenium_url or search_url
                else:
                    logger.error(f"Surugaya search Selenium fallback error: {selenium_error}")

        if soup is None:
            if first_resp is not None and _is_cloudflare_block(first_resp):
                print(f"[SURUGAYA] Search: ERROR: Cloudflare block detected (Status: {first_resp.status_code})")
            elif fetch_error is not None:
                logger.error(f"Surugaya search fetch error: {fetch_error}")
            else:
                logger.error("Surugaya search fetch error: unable to fetch page")
            if _should_use_yahoo_search_fallback():
                print("[SURUGAYA] Search: INFO: Trying Yahoo search fallback after blocked search page...")
                product_urls = _search_product_urls_via_yahoo(keyword, max_items=max_items)
        else:
            print(f"[SURUGAYA] Search: Page title: {soup.title.string if soup.title else 'No Title'}")

            if _looks_like_challenge_soup(soup):
                logger.warning("Surugaya search page appears to be a challenge page.")
                if _should_use_yahoo_search_fallback():
                    print("[SURUGAYA] Search: INFO: Trying Yahoo search fallback for product URLs...")
                    product_urls = _search_product_urls_via_yahoo(keyword, max_items=max_items)

            page_urls = _build_search_page_urls(base_search_url, soup, max_scroll=max_scroll)

            for index, page_url in enumerate(page_urls):
                if len(product_urls) >= max_items:
                    break

                if index == 0:
                    page_soup = soup
                else:
                    page_resp, page_error = _fetch_with_retry(session, page_url, timeout=30, max_attempts=2)
                    page_soup = None
                    if page_error is None and page_resp is not None and not _is_cloudflare_block(page_resp):
                        page_soup = BeautifulSoup(page_resp.content, "html.parser")
                    elif _should_use_selenium_fallback():
                        selenium_soup, _, selenium_error = _fetch_soup_with_selenium(
                            page_url,
                            headless=headless,
                        )
                        if selenium_soup is not None:
                            page_soup = selenium_soup
                        else:
                            logger.warning(f"Surugaya page Selenium fallback failed: {page_url} ({selenium_error})")

                    if page_soup is None:
                        if page_error is not None:
                            logger.warning(f"Surugaya page fetch failed: {page_url} ({page_error})")
                        else:
                            logger.warning(f"Surugaya page blocked/skipped: {page_url}")
                        continue

                    if _looks_like_challenge_soup(page_soup):
                        logger.warning(f"Surugaya page challenge detected: {page_url}")
                        continue

                for product_url in _extract_product_urls(page_soup, page_url):
                    if product_url in product_urls:
                        continue
                    product_urls.append(product_url)
                    if len(product_urls) >= max_items:
                        break

                if len(product_urls) >= max_items:
                    break

            if not product_urls and _should_use_yahoo_search_fallback():
                print("[SURUGAYA] Search: INFO: Trying Yahoo search fallback (no product links found)...")
                product_urls = _search_product_urls_via_yahoo(keyword, max_items=max_items)

        if not product_urls:
            return results

        # Scrape each product detail
        for url in product_urls[:max_items]:
            try:
                result = scrape_item_detail(session, url, headless=headless)
                if result["title"]:
                    results.append(result)
            except Exception as e:
                logger.error(f"Error scraping {url}: {e}")
                continue

        return results

    except Exception as e:
        logger.error(f"Error in scrape_search_result: {e}")
        return results
