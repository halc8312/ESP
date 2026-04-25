"""
Shared page-state classification helpers for selector healing.

The classifier is intentionally conservative. It only recognizes a small
set of healthy detail-page signals for Mercari and SNKRDUNK so abnormal
pages do not trigger selector healing or selector promotion.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


_GENERIC_BLOCKED_MARKERS = (
    "access denied",
    "forbidden",
    "too many requests",
    "temporarily blocked",
    "unusual traffic",
    "request blocked",
)
_GENERIC_CHALLENGE_MARKERS = (
    "verify you are human",
    "are you human",
    "attention required",
    "security check",
    "captcha",
    "cf-chl",
    "challenge",
)
_GENERIC_LOGIN_MARKERS = (
    "ログイン",
    "会員登録",
    "sign in",
    "log in",
    "login",
)

_MERCARI_MISSING_PAGE_MARKERS = (
    "該当する商品は削除されています",
    "この商品は削除されています",
    "お探しの商品は見つかりません",
    "商品は存在しません",
)
_MERCARI_HOME_TITLES = {
    "メルカリ - 日本最大のフリマサービス",
    "mercari: your marketplace",
}
_MERCARI_ACTIVE_MARKERS = ("購入手続きへ", "購入する", "buy this item")
_MERCARI_SOLD_MARKERS = ("売り切れ", "売り切れました", "sold")

_SNKRDUNK_HOME_TITLES = {
    "snkrdunk",
    "スニーカーダンク",
    "スニダン",
}
_SNKRDUNK_SOLD_MARKERS = ("sold out", "売り切れ", "在庫なし")


@dataclass(frozen=True)
class PageStateAssessment:
    state: str
    allow_healing: bool
    reasons: tuple[str, ...] = ()


def _safe_css(page, selector: str) -> list:
    try:
        return list(page.css(selector))
    except Exception:
        return []


def _node_text(node) -> str:
    text = getattr(node, "text", "")
    return text.strip() if isinstance(text, str) else ""


def _node_attr(node, attr: str) -> str:
    attrib = getattr(node, "attrib", {}) or {}
    value = attrib.get(attr, "")
    return value.strip() if isinstance(value, str) else ""


def _extract_page_title(page) -> str:
    title_nodes = _safe_css(page, "title")
    if title_nodes:
        return _node_text(title_nodes[0])
    return ""


def _extract_body_text(page) -> str:
    for attr_name in ("get_all_text", "get_text"):
        extractor = getattr(page, attr_name, None)
        if not callable(extractor):
            continue
        try:
            text = extractor() or ""
        except Exception:
            continue
        if isinstance(text, str) and text.strip():
            return text.strip()

    body_nodes = _safe_css(page, "body")
    if body_nodes:
        return _node_text(body_nodes[0])

    return ""


def _extract_first_text(page, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        nodes = _safe_css(page, selector)
        if nodes:
            text = _node_text(nodes[0])
            if text:
                return text
    return ""


def _extract_first_meta_content(page, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        nodes = _safe_css(page, selector)
        if nodes:
            content = _node_attr(nodes[0], "content")
            if content:
                return content
    return ""


def _extract_price_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    for pattern in (r"[¥￥]\s*([\d,]+)", r"([\d,]+)\s*円"):
        match = re.search(pattern, text)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group(1) or "")
        if not digits:
            continue
        try:
            return int(digits)
        except ValueError:
            continue
    return None


def _extract_product_jsonld(page) -> Optional[dict]:
    scripts = _safe_css(page, "script[type='application/ld+json']")

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
                types = [str(item).lower() for item in node_type]
            else:
                types = [str(node_type).lower()] if node_type else []
            if "product" in types:
                return node
            for key in ("@graph", "mainEntity", "item", "product", "data"):
                nested = node.get(key)
                if nested is not None:
                    found = _find_product(nested)
                    if found:
                        return found
        return None

    for script in scripts:
        raw = _node_text(script)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _find_product(parsed)
        if found:
            return found
    return None


def _extract_next_data_item(page) -> Optional[dict]:
    script = None
    for selector in ("#__NEXT_DATA__", "script#__NEXT_DATA__"):
        nodes = _safe_css(page, selector)
        if nodes:
            script = nodes[0]
            break
    if script is None:
        return None

    raw = _node_text(script)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    props = data.get("props", {})
    page_props = props.get("pageProps", {})
    item = (
        page_props.get("item")
        or page_props.get("product")
        or page_props.get("initialState", {}).get("item", {})
        or page_props.get("initialState", {}).get("product", {})
        or {}
    )
    return item if isinstance(item, dict) and item else None


def _status_code(page) -> int:
    try:
        return int(getattr(page, "status", 200) or 200)
    except Exception:
        return 200


def _text_contains_any(text: str, markers: tuple[str, ...]) -> Optional[str]:
    haystack = str(text or "").lower()
    for marker in markers:
        if marker.lower() in haystack:
            return marker
    return None


def _has_login_gate(page, combined_text: str, *, product_signals: bool) -> bool:
    if product_signals:
        return False
    password_inputs = _safe_css(
        page,
        "input[type='password'], input[name*='password'], input[autocomplete='current-password']",
    )
    if not password_inputs:
        return False
    if _text_contains_any(combined_text, _GENERIC_LOGIN_MARKERS):
        return True
    return bool(_safe_css(page, "form[action*='login'], form[action*='signin'], form[action*='session']"))


def _has_challenge_gate(page, combined_text: str, *, product_signals: bool) -> bool:
    if product_signals:
        return False
    if _text_contains_any(combined_text, _GENERIC_CHALLENGE_MARKERS):
        return True
    return bool(
        _safe_css(
            page,
            "iframe[src*='captcha'], [id*='captcha'], [class*='captcha'], [id*='challenge'], [class*='challenge']",
        )
    )


def _has_block_gate(status_code: int, combined_text: str, *, product_signals: bool) -> bool:
    if status_code in {401, 403, 429} and not product_signals:
        return True
    if product_signals:
        return False
    return _text_contains_any(combined_text, _GENERIC_BLOCKED_MARKERS) is not None


def _normalize_snkrdunk_title(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"の新品/中古.*$", "", normalized).strip()
    normalized = re.sub(r"\s*[|｜]\s*(スニダン|snkrdunk)\s*$", "", normalized, flags=re.IGNORECASE).strip()
    return normalized


def _title_looks_like_gate(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    return any(
        marker.lower() in normalized
        for marker in (_GENERIC_LOGIN_MARKERS + _GENERIC_CHALLENGE_MARKERS + _GENERIC_BLOCKED_MARKERS)
    )


def _classify_mercari_detail_page(page) -> PageStateAssessment:
    reasons: list[str] = []
    body_text = _extract_body_text(page)
    page_title = _extract_page_title(page).strip()
    status_code = _status_code(page)
    title = _extract_first_text(page, ("h1", "[data-testid='item-name']", "[data-testid='name']"))
    meta_price = _extract_price_from_text(
        _extract_first_meta_content(page, ("meta[name='product:price:amount']", "meta[property='product:price:amount']"))
    )
    dom_price = _extract_price_from_text(
        _extract_first_text(page, ("[data-testid='price']", "main [data-testid='price']", "article [data-testid='price']"))
    )
    product_jsonld = _extract_product_jsonld(page)
    image_hint = _extract_first_meta_content(page, ("meta[property='og:image']",))
    has_product_signal = any(
        (
            bool(title),
            meta_price is not None,
            dom_price is not None,
            isinstance(product_jsonld, dict),
            "mercdn.net" in image_hint.lower(),
            _text_contains_any(body_text, _MERCARI_ACTIVE_MARKERS) is not None,
            _text_contains_any(body_text, _MERCARI_SOLD_MARKERS) is not None,
        )
    )
    combined_text = f"{page_title}\n{body_text}"

    missing_marker = next((marker for marker in _MERCARI_MISSING_PAGE_MARKERS if marker in body_text), "")
    if missing_marker:
        reasons.append(f"missing-marker:{missing_marker}")
        return PageStateAssessment(state="deleted", allow_healing=False, reasons=tuple(reasons))

    if _has_challenge_gate(page, combined_text, product_signals=has_product_signal):
        reasons.append("challenge-gate")
        return PageStateAssessment(state="challenge", allow_healing=False, reasons=tuple(reasons))

    if _has_block_gate(status_code, combined_text, product_signals=has_product_signal):
        reasons.append(f"blocked-status:{status_code}" if status_code in {401, 403, 429} else "blocked-marker")
        return PageStateAssessment(state="blocked", allow_healing=False, reasons=tuple(reasons))

    if _has_login_gate(page, combined_text, product_signals=has_product_signal):
        reasons.append("login-gate")
        return PageStateAssessment(state="login_required", allow_healing=False, reasons=tuple(reasons))

    if not has_product_signal and page_title.lower() in _MERCARI_HOME_TITLES:
        reasons.append("home-title-without-product-signals")
        return PageStateAssessment(state="deleted", allow_healing=False, reasons=tuple(reasons))

    if has_product_signal:
        reasons.append("mercari-product-signals")
        return PageStateAssessment(state="healthy", allow_healing=True, reasons=tuple(reasons))

    reasons.append("no-product-signals")
    return PageStateAssessment(state="unknown", allow_healing=False, reasons=tuple(reasons))


def _classify_snkrdunk_detail_page(page) -> PageStateAssessment:
    reasons: list[str] = []
    body_text = _extract_body_text(page)
    page_title = _extract_page_title(page).strip()
    status_code = _status_code(page)
    next_data_item = _extract_next_data_item(page)
    product_jsonld = _extract_product_jsonld(page)
    meta_title = _normalize_snkrdunk_title(
        _extract_first_meta_content(page, ("meta[property='og:title']", "meta[name='twitter:title']"))
        or page_title
    )
    meta_price = _extract_price_from_text(
        _extract_first_meta_content(page, ("meta[name='product:price:amount']", "meta[property='product:price:amount']"))
    )
    page_text_price = _extract_price_from_text(body_text)
    image_hint = _extract_first_meta_content(page, ("meta[property='og:image']",))
    has_sold_marker = _text_contains_any(body_text, _SNKRDUNK_SOLD_MARKERS) is not None
    has_product_signal = any(
        (
            isinstance(next_data_item, dict),
            isinstance(product_jsonld, dict),
            bool(meta_title) and meta_title.lower() not in _SNKRDUNK_HOME_TITLES and not _title_looks_like_gate(meta_title),
            meta_price is not None,
            page_text_price is not None,
            bool(image_hint),
            has_sold_marker,
        )
    )
    combined_text = f"{page_title}\n{body_text}"

    if _has_challenge_gate(page, combined_text, product_signals=has_product_signal):
        reasons.append("challenge-gate")
        return PageStateAssessment(state="challenge", allow_healing=False, reasons=tuple(reasons))

    if _has_block_gate(status_code, combined_text, product_signals=has_product_signal):
        reasons.append(f"blocked-status:{status_code}" if status_code in {401, 403, 429} else "blocked-marker")
        return PageStateAssessment(state="blocked", allow_healing=False, reasons=tuple(reasons))

    if _has_login_gate(page, combined_text, product_signals=has_product_signal):
        reasons.append("login-gate")
        return PageStateAssessment(state="login_required", allow_healing=False, reasons=tuple(reasons))

    if has_product_signal:
        reasons.append("snkrdunk-product-signals")
        return PageStateAssessment(state="healthy", allow_healing=True, reasons=tuple(reasons))

    reasons.append("no-product-signals")
    return PageStateAssessment(state="unknown", allow_healing=False, reasons=tuple(reasons))


def classify_page_state(site: str, page, page_type: str = "detail") -> PageStateAssessment:
    normalized_site = str(site or "").strip().lower()
    normalized_page_type = str(page_type or "").strip().lower()

    if normalized_page_type != "detail":
        return PageStateAssessment(state="unclassified", allow_healing=True, reasons=("page-type-unsupported",))

    if normalized_site == "mercari":
        return _classify_mercari_detail_page(page)
    if normalized_site == "snkrdunk":
        return _classify_snkrdunk_detail_page(page)

    return PageStateAssessment(state="unclassified", allow_healing=True, reasons=("site-unsupported",))
