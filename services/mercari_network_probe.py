"""
Live Mercari network probe helpers.

This module is intentionally diagnostic-only. It does not change scrape
behavior; it captures current browser-visible JSON responses and summarizes
whether they expose status-like fields that could harden stock detection.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urljoin

from services.browser_pool import run_browser_page_task
from services.mercari_item_parser import parse_mercari_network_payload


_DETAIL_WAIT_SELECTOR = "h1, [data-testid='price'], [data-testid='checkout-button']"
_SEARCH_WAIT_SELECTOR = "a[data-testid='thumbnail-link'], a[href*='/item/']"
_SEARCH_LINK_SELECTORS = (
    "a[data-testid='thumbnail-link']",
    "a[href*='/item/']",
    "li[data-testid='item-cell'] a",
)
_DEFAULT_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
)
_DEFAULT_CONTEXT_OPTIONS = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "extra_http_headers": {"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
}
_STATUS_KEYS = frozenset(
    {
        "status",
        "itemStatus",
        "saleStatus",
        "availability",
        "soldOut",
        "isSoldOut",
        "state",
        "deleted",
        "isDeleted",
        "transactionStatus",
        "listingStatus",
    }
)
_SUMMARY_ID_KEYS = ("id", "itemId", "item_id", "name", "title")
_HTML_STATUS_TOKENS = (
    "__NEXT_DATA__",
    "application/ld+json",
    "self.__next_f.push",
    "購入手続きへ",
    "売り切れました",
    "売り切れ",
    "OutOfStock",
    "InStock",
    "出品を停止",
)


def _compact_scalar(value: Any, max_length: int = 120) -> Any:
    if isinstance(value, str):
        normalized = " ".join(value.split())
        if len(normalized) > max_length:
            return normalized[: max_length - 3] + "..."
        return normalized
    return value


def _compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _compact_scalar(nested)
            for key, nested in list(value.items())[:6]
        }
    if isinstance(value, list):
        return [_compact_scalar(item) for item in value[:6]]
    return _compact_scalar(value)


def _has_usable_payload_item(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if isinstance(item.get("title"), str) and item["title"].strip():
        return True
    if isinstance(item.get("price"), int) and item["price"] > 0:
        return True
    if item.get("status") in {"on_sale", "sold", "deleted"}:
        return True
    if isinstance(item.get("image_urls"), list) and item["image_urls"]:
        return True
    return False


def collect_status_records(
    payload: Any,
    *,
    max_records: int = 40,
    max_nodes: int = 600,
    max_depth: int = 12,
) -> list[dict[str, Any]]:
    """Collect nested dict nodes that expose status-like fields."""
    records: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    nodes_seen = 0

    def _append_record(path: str, node: dict[str, Any]) -> None:
        fields = {}
        for key in _STATUS_KEYS:
            if key not in node:
                continue
            value = node.get(key)
            if value in (None, "", [], {}):
                continue
            fields[key] = _compact_value(value)
        if not fields:
            return

        record: dict[str, Any] = {"path": path, "fields": fields}
        for key in _SUMMARY_ID_KEYS:
            if key in node and node.get(key) not in (None, ""):
                record[key] = _compact_scalar(node.get(key))

        signature = repr(record)
        if signature in seen_records:
            return
        seen_records.add(signature)
        records.append(record)

    def _walk(value: Any, path: str, depth: int) -> None:
        nonlocal nodes_seen
        if len(records) >= max_records or nodes_seen >= max_nodes or depth > max_depth:
            return
        nodes_seen += 1

        if isinstance(value, dict):
            _append_record(path, value)
            for key, nested in value.items():
                if len(records) >= max_records or nodes_seen >= max_nodes:
                    break
                _walk(nested, f"{path}.{key}", depth + 1)
            return

        if isinstance(value, list):
            for index, nested in enumerate(value[:50]):
                if len(records) >= max_records or nodes_seen >= max_nodes:
                    break
                _walk(nested, f"{path}[{index}]", depth + 1)

    _walk(payload, "$", 0)
    return records


def _count_status_values(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        fields = record.get("fields") or {}
        for value in fields.values():
            if isinstance(value, list):
                values = value
            else:
                values = [value]
            for nested in values:
                if isinstance(nested, (dict, list)):
                    continue
                label = str(nested or "").strip()
                if not label:
                    continue
                counts[label] = counts.get(label, 0) + 1
    return counts


def summarize_html_signals(html: str) -> dict[str, Any]:
    normalized_html = html or ""
    matched_tokens = [token for token in _HTML_STATUS_TOKENS if token in normalized_html]
    return {
        "has_next_data": "__NEXT_DATA__" in normalized_html,
        "has_jsonld": "application/ld+json" in normalized_html,
        "has_next_flight": "self.__next_f.push" in normalized_html,
        "matched_tokens": matched_tokens,
    }


def summarize_payload(payload: Any, page_url: str) -> dict[str, Any]:
    status_records = collect_status_records(payload)
    summary: dict[str, Any] = {
        "top_level_keys": list(payload.keys())[:20] if isinstance(payload, dict) else [],
        "status_records": status_records,
        "status_value_counts": _count_status_values(status_records),
        "parsed_item": None,
        "parsed_meta": None,
    }

    try:
        item, meta = parse_mercari_network_payload(payload, page_url)
    except Exception as exc:  # pragma: no cover - diagnostic guard
        summary["parse_error"] = str(exc)
        return summary

    if _has_usable_payload_item(item):
        summary["parsed_item"] = {
            "title": item.get("title") or "",
            "price": item.get("price"),
            "status": item.get("status") or "unknown",
            "image_count": len(item.get("image_urls") or []),
        }
        summary["parsed_meta"] = {
            "strategy": str((meta or {}).get("strategy") or ""),
            "reasons": list((meta or {}).get("reasons") or []),
            "field_sources": dict((meta or {}).get("field_sources") or {}),
        }
    return summary


def _response_usefulness_score(summary: dict[str, Any]) -> int:
    parsed_item = summary.get("parsed_item") or {}
    score = 0
    if parsed_item.get("title"):
        score += 4
    if parsed_item.get("price"):
        score += 3
    if parsed_item.get("status") in {"on_sale", "sold", "deleted"}:
        score += 3
    if parsed_item.get("image_count"):
        score += 2
    score += min(len(summary.get("status_records") or []), 5)
    return score


async def _collect_search_item_urls(page) -> list[str]:
    item_urls: list[str] = []
    for selector in _SEARCH_LINK_SELECTORS:
        try:
            links = await page.query_selector_all(selector)
        except Exception:
            continue
        for link in links[:80]:
            try:
                href = await link.get_attribute("href")
            except Exception:
                href = ""
            if not href or "/item/" not in href:
                continue
            normalized = urljoin("https://jp.mercari.com", href)
            if normalized not in item_urls:
                item_urls.append(normalized)
        if item_urls:
            break
    return item_urls


async def probe_mercari_page(
    url: str,
    *,
    page_kind: str = "detail",
    max_responses: int = 25,
    include_raw_payloads: bool = False,
) -> dict[str, Any]:
    """Open a Mercari page in a real browser and summarize JSON responses."""
    captured_responses: list[dict[str, Any]] = []
    response_tasks: list[asyncio.Task] = []

    async def _task(page, context):
        async def _capture_response(response) -> None:
            if len(captured_responses) >= max_responses:
                return

            try:
                headers = await response.all_headers()
            except Exception:
                headers = {}

            response_url = str(getattr(response, "url", "") or "")
            content_type = str(headers.get("content-type", "") or "").lower()
            if "mercari" not in response_url.lower():
                return
            if "json" not in content_type and not response_url.lower().endswith(".json"):
                return

            try:
                payload = await response.json()
            except Exception:
                return

            summary = summarize_payload(payload, url)
            captured_responses.append(
                {
                    "url": response_url,
                    "status": int(getattr(response, "status", 0) or 0),
                    "content_type": content_type,
                    "summary": summary,
                    "payload": payload if include_raw_payloads else None,
                }
            )

        page.on(
            "response",
            lambda response: response_tasks.append(asyncio.create_task(_capture_response(response))),
        )

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        wait_selector = _DETAIL_WAIT_SELECTOR if page_kind == "detail" else _SEARCH_WAIT_SELECTOR
        try:
            await page.wait_for_selector(wait_selector, timeout=8000)
        except Exception:
            pass

        if page_kind == "search":
            await page.wait_for_timeout(1500)

        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)

        html = await page.content()
        title = await page.title()
        item_urls = await _collect_search_item_urls(page) if page_kind == "search" else []
        return {
            "final_url": page.url,
            "title": title,
            "html": html,
            "item_urls": item_urls,
        }

    page_result = await run_browser_page_task(
        "mercari",
        _task,
        headless=True,
        launch_args=_DEFAULT_LAUNCH_ARGS,
        context_options=_DEFAULT_CONTEXT_OPTIONS,
    )

    summarized_responses = []
    for index, response in enumerate(captured_responses):
        summary = dict(response.get("summary") or {})
        summarized_responses.append(
            {
                "capture_index": index,
                "url": response.get("url") or "",
                "status": response.get("status") or 0,
                "content_type": response.get("content_type") or "",
                "usefulness_score": _response_usefulness_score(summary),
                "summary": summary,
                **({"payload": response.get("payload")} if include_raw_payloads else {}),
            }
        )

    summarized_responses.sort(
        key=lambda entry: (entry.get("usefulness_score", 0), -entry.get("capture_index", 0)),
        reverse=True,
    )

    html = str(page_result.get("html") or "")
    return {
        "target_url": url,
        "page_kind": page_kind,
        "final_url": page_result.get("final_url") or url,
        "title": page_result.get("title") or "",
        "html_signals": summarize_html_signals(html),
        "search_item_urls": page_result.get("item_urls") or [],
        "captured_response_count": len(summarized_responses),
        "responses": summarized_responses,
    }
