"""
Best-effort product extraction from sites outside the supported marketplaces.

Most shops publish enough structured data (schema.org Product/Offer in JSON-LD,
or Open Graph meta tags) to recover a title, price and image without a
site-specific parser. That will never be complete, so this reports what it
could not find alongside what it could, and the caller hands both to the manual
add form for the operator to finish.

Security note: the supported marketplaces are behind an exact host allowlist,
which makes server-side request forgery impossible by construction. Accepting
arbitrary URLs removes that guarantee, so every host is resolved and checked
against private address ranges before a request is made, and every redirect hop
is re-checked — a public hostname is free to redirect at an internal address.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from services.scrape_safety import UnsafeScrapeUrlError

logger = logging.getLogger(__name__)

GENERIC_SITE = "other"
GENERIC_SITE_LABEL = "その他のサイト（自動判定）"

FETCH_TIMEOUT_SECONDS = 20
#: Product pages are HTML; anything beyond this is not a page we can parse.
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

FIELD_LABELS = {
    "title": "商品名",
    "price": "価格",
    "image_urls": "画像",
    "description": "商品説明",
    "availability": "在庫",
}


def _reject(message):
    raise UnsafeScrapeUrlError(message)


def _resolved_addresses(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeScrapeUrlError("URLのホスト名を解決できませんでした。") from exc

    addresses = []
    for info in infos:
        raw = info[4][0]
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    if not addresses:
        _reject("URLのホスト名を解決できませんでした。")
    return addresses


def validate_generic_product_url(url: str) -> str:
    """
    Return the URL if it is safe to fetch, else raise UnsafeScrapeUrlError.

    Rejects anything that is not plain HTTPS on the default port, embedded
    credentials, bare IP literals, and any hostname that resolves to a
    non-public address.
    """
    raw = str(url or "").strip()
    if not raw:
        _reject("URLを入力してください。")

    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise UnsafeScrapeUrlError("URLの形式を確認してください。") from exc

    if parsed.scheme.lower() != "https":
        _reject("URLは https:// で始まる形式で入力してください。")
    if parsed.username is not None or parsed.password is not None:
        _reject("ユーザー名やパスワードを含むURLは使用できません。")

    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeScrapeUrlError("URLのポート番号を確認してください。") from exc
    if port not in (None, 443):
        _reject("標準のHTTPSポート以外を指定したURLは使用できません。")

    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        _reject("URLのホスト名を確認してください。")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _reject("IPアドレスを直接指定したURLは使用できません。")

    for address in _resolved_addresses(host):
        if not address.is_global or address.is_multicast:
            _reject("社内ネットワーク向けのURLは使用できません。")

    return raw


def _fetch_html(url: str) -> tuple[str, str]:
    """Fetch a page, re-validating every redirect hop. Returns (html, final_url)."""
    current_url = validate_generic_product_url(url)

    session = requests.Session()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            response = session.get(
                current_url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=FETCH_TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
            )
            try:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location", "")
                    if not location:
                        _reject("転送先のURLを確認できませんでした。")
                    # Re-validate: a public host is free to redirect inward.
                    current_url = validate_generic_product_url(
                        urljoin(current_url, location)
                    )
                    continue

                response.raise_for_status()

                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type and "html" not in content_type:
                    raise ValueError("HTMLページではありません")

                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=16384):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise ValueError("ページが大きすぎます")

                encoding = response.encoding or response.apparent_encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace"), current_url
            finally:
                response.close()

        _reject("転送が多すぎます。")
    finally:
        session.close()


def _parse_price(raw_value):
    """Return an integer yen amount from the many shapes prices come in."""
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return int(raw_value) if raw_value > 0 else None

    text = str(raw_value).strip()
    if not text:
        return None
    # Full-width digits and separators are common on Japanese shops. Without
    # the separators, "￥４，８００" reads as 4.
    text = text.translate(
        str.maketrans("０１２３４５６７８９，．", "0123456789,.")
    )
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return int(value) if value > 0 else None


def _iter_json_ld_nodes(soup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue

        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)


def _node_types(node):
    raw_type = node.get("@type")
    if isinstance(raw_type, str):
        return {raw_type.lower()}
    if isinstance(raw_type, list):
        return {str(item).lower() for item in raw_type}
    return set()


def _first_offer(node):
    offers = node.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                return offer
    return None


def _extract_from_json_ld(soup, result):
    for node in _iter_json_ld_nodes(soup):
        if "product" not in _node_types(node):
            continue

        if not result["title"] and isinstance(node.get("name"), str):
            result["title"] = node["name"].strip()

        if not result["description"] and isinstance(node.get("description"), str):
            result["description"] = node["description"].strip()

        image = node.get("image")
        if not result["image_urls"] and image:
            if isinstance(image, str):
                result["image_urls"] = [image]
            elif isinstance(image, list):
                result["image_urls"] = [item for item in image if isinstance(item, str)]
            elif isinstance(image, dict) and isinstance(image.get("url"), str):
                result["image_urls"] = [image["url"]]

        offer = _first_offer(node)
        if offer:
            if result["price"] is None:
                result["price"] = _parse_price(offer.get("price"))
            if not result["currency"] and isinstance(offer.get("priceCurrency"), str):
                result["currency"] = offer["priceCurrency"].strip().upper()
            availability = offer.get("availability")
            if not result["availability"] and isinstance(availability, str):
                result["availability"] = availability.rsplit("/", 1)[-1]

        if result["title"] and result["price"] is not None:
            break


def _meta_content(soup, *, prop=None, name=None):
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _extract_from_open_graph(soup, result):
    if not result["title"]:
        result["title"] = _meta_content(soup, prop="og:title", name="twitter:title")

    if not result["description"]:
        result["description"] = _meta_content(
            soup, prop="og:description", name="description"
        )

    if not result["image_urls"]:
        image = _meta_content(soup, prop="og:image", name="twitter:image")
        if image:
            result["image_urls"] = [image]

    if result["price"] is None:
        result["price"] = _parse_price(
            _meta_content(soup, prop="product:price:amount")
            or _meta_content(soup, prop="og:price:amount")
        )

    if not result["currency"]:
        result["currency"] = (
            _meta_content(soup, prop="product:price:currency")
            or _meta_content(soup, prop="og:price:currency")
        ).upper()

    if not result["availability"]:
        result["availability"] = _meta_content(soup, prop="product:availability")


def extract_product_fields(html: str, page_url: str) -> dict:
    """Pull what we can out of a product page's structured data."""
    soup = BeautifulSoup(html, "html.parser")
    result = {
        "title": "",
        "price": None,
        "currency": "",
        "image_urls": [],
        "description": "",
        "availability": "",
        "source_url": page_url,
    }

    _extract_from_json_ld(soup, result)
    _extract_from_open_graph(soup, result)

    if not result["title"] and soup.title and soup.title.string:
        result["title"] = soup.title.string.strip()

    # Structured data often carries protocol-relative or site-relative URLs.
    result["image_urls"] = [
        urljoin(page_url, str(url).strip())
        for url in result["image_urls"]
        if str(url).strip()
    ]

    # A non-JPY price would be wrong in a yen-only admin, so it is reported as
    # not found rather than silently mis-filled.
    if result["price"] is not None and result["currency"] and result["currency"] != "JPY":
        result["price"] = None
        result["currency_warning"] = True
    else:
        result["currency_warning"] = False

    found = [field for field in FIELD_LABELS if _has_value(result, field)]
    result["found_fields"] = found
    result["missing_fields"] = [field for field in FIELD_LABELS if field not in found]
    return result


def _has_value(result, field):
    value = result.get(field)
    if field == "price":
        return value is not None
    if field == "image_urls":
        return bool(value)
    return bool(str(value or "").strip())


def fetch_generic_product(url: str) -> dict:
    """
    Fetch and parse a product page from an unsupported site.

    Returns {"status": "ok", "product": {...}} or {"status": "failed",
    "error": "..."}; nothing here raises, because every failure ends the same
    way for the operator — fall back to filling the form by hand.
    """
    try:
        html, final_url = _fetch_html(url)
    except UnsafeScrapeUrlError as exc:
        return {"status": "failed", "error": str(exc), "product": None}
    except requests.RequestException as exc:
        logger.info("Generic product fetch failed for %s: %s", url, exc)
        return {
            "status": "failed",
            "error": "ページを読み取れませんでした。サイトが応答しないか、アクセスが制限されています。",
            "product": None,
        }
    except ValueError as exc:
        return {"status": "failed", "error": f"ページを読み取れませんでした（{exc}）。", "product": None}

    product = extract_product_fields(html, final_url)
    if not product["title"]:
        return {
            "status": "failed",
            "error": "このページからは商品名を読み取れませんでした。手動追加からご登録ください。",
            "product": product,
        }
    return {"status": "ok", "error": None, "product": product}
