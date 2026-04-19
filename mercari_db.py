import logging
import asyncio
import re
import os
import shutil
import uuid

# Import selector configuration loader
try:
    from selector_config import get_selectors
except ImportError:
    # Fallback if module not found
    def get_selectors(site, page_type, field):
        return []

# Import self-healing engine
try:
    from services.selector_healer import get_healer
except ImportError:
    get_healer = None

from services.mercari_item_parser import parse_mercari_item_page, _append_unique_image_url
from services.mercari_item_parser import (
    collect_mercari_photo_urls_for_item,
    extract_mercari_item_id,
    parse_mercari_network_payload,
)
from services.extraction_policy import attach_extraction_trace, pick_first_valid
from services.browser_pool import run_browser_page_task
from services.html_page_adapter import HtmlPageAdapter
from services.mercari_browser_fetch import (
    fetch_mercari_page_and_payloads_via_browser_pool_sync,
    should_use_mercari_browser_pool_detail,
)
from services.scraping_client import fetch_dynamic, gather_with_concurrency, get_async_fetch_settings, run_coro_sync
from services.scrape_alerts import report_detail_result

# Import metrics logging
try:
    from scrape_metrics import get_metrics, log_scrape_result, check_scrape_health
except ImportError:
    # Fallback if module not found
    def get_metrics():
        class DummyMetrics:
            def start(self, *a): pass
            def record_attempt(self, *a): pass
            def finish(self): return {}
        return DummyMetrics()
    def log_scrape_result(*a): return True
    def check_scrape_health(*a): return {"action_required": False}


logger = logging.getLogger("mercari")


def _env_flag(name: str) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _should_capture_mercari_network_payload() -> bool:
    return _env_flag("MERCARI_CAPTURE_NETWORK_PAYLOAD") or _should_use_mercari_network_payload()


def _should_use_mercari_network_payload() -> bool:
    return _env_flag("MERCARI_USE_NETWORK_PAYLOAD")


def _is_nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_price(value) -> bool:
    return isinstance(value, int) and value > 0


def _is_nonempty_list(value) -> bool:
    return isinstance(value, list) and len(value) > 0


def _is_useful_status(value) -> bool:
    return value in {"on_sale", "sold", "deleted"}


def _dedupe_str_list(values) -> list[str]:
    urls = []
    if not isinstance(values, list):
        return urls

    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _merge_source_labels(*labels: str) -> str:
    parts = []
    seen = set()
    for label in labels:
        for part in str(label or "").split("+"):
            normalized = part.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            parts.append(normalized)
    return "+".join(parts)


def _merge_image_lists(payload_urls, payload_source: str, dom_urls, dom_source: str) -> tuple[list, str]:
    payload_list = _dedupe_str_list(payload_urls)
    dom_list = _dedupe_str_list(dom_urls)

    if not payload_list:
        return dom_list, dom_source
    if not dom_list:
        return payload_list, payload_source

    if len(payload_list) >= len(dom_list):
        primary_urls, primary_source = payload_list, payload_source
        secondary_urls, secondary_source = dom_list, dom_source
    else:
        primary_urls, primary_source = dom_list, dom_source
        secondary_urls, secondary_source = payload_list, payload_source

    merged_urls = list(primary_urls)
    for url in secondary_urls:
        if url not in merged_urls:
            merged_urls.append(url)

    if len(merged_urls) == len(primary_urls):
        return primary_urls, primary_source

    return merged_urls, _merge_source_labels(primary_source, secondary_source)


def _normalize_shadow_value(field: str, value):
    if field == "description" and isinstance(value, str):
        return " ".join(value.split())
    if field == "image_urls" and isinstance(value, list):
        return list(value)
    return value


def _build_shadow_compare(payload_item: dict, dom_item: dict) -> dict:
    compared_fields = []
    mismatch_fields = []
    for field, validator in (
        ("title", _is_nonempty_text),
        ("price", _is_positive_price),
        ("description", _is_nonempty_text),
        ("image_urls", _is_nonempty_list),
        ("status", _is_useful_status),
    ):
        payload_value = payload_item.get(field)
        dom_value = dom_item.get(field)
        if not validator(payload_value) or not validator(dom_value):
            continue
        compared_fields.append(field)
        if _normalize_shadow_value(field, payload_value) != _normalize_shadow_value(field, dom_value):
            mismatch_fields.append(field)
    return {
        "payload_available": any(
            (
                _is_nonempty_text(payload_item.get("title")),
                _is_positive_price(payload_item.get("price")),
                _is_nonempty_text(payload_item.get("description")),
                _is_nonempty_list(payload_item.get("image_urls")),
            )
        ),
        "compared_fields": compared_fields,
        "mismatch_fields": mismatch_fields,
    }


def _has_usable_payload_item(payload_item: dict) -> bool:
    return any(
        (
            _is_nonempty_text(payload_item.get("title")),
            _is_positive_price(payload_item.get("price")),
            _is_nonempty_text(payload_item.get("description")),
            _is_nonempty_list(payload_item.get("image_urls")),
            _is_useful_status(payload_item.get("status")),
        )
    )


def _build_mercari_dom_meta(item: dict, meta: dict) -> dict:
    merged_meta = dict(meta or {})
    merged_meta.setdefault("strategy", meta.get("strategy", "dom") if isinstance(meta, dict) else "dom")
    merged_meta.setdefault("field_sources", {})
    merged_meta["field_sources"] = dict(merged_meta.get("field_sources") or {})
    if item.get("title"):
        merged_meta["field_sources"].setdefault("title", "dom")
    if item.get("description"):
        merged_meta["field_sources"].setdefault("description", "dom")
    if item.get("image_urls"):
        merged_meta["field_sources"].setdefault("image_urls", "dom")
    if item.get("variants"):
        merged_meta["field_sources"].setdefault("variants", "dom")
    if item.get("status"):
        merged_meta["field_sources"].setdefault("status", merged_meta["field_sources"].get("status") or "dom")
    return merged_meta


def _normalize_mercari_detail_page(page, url: str):
    if isinstance(page, HtmlPageAdapter):
        return page

    raw_html = getattr(page, "body", "")
    if isinstance(raw_html, bytes):
        raw_html = raw_html.decode("utf-8", errors="ignore")
    if isinstance(raw_html, str) and raw_html.strip():
        return HtmlPageAdapter(
            raw_html,
            url=str(getattr(page, "url", "") or url),
            status=int(getattr(page, "status", 200) or 200),
        )

    return page


def _count_image_urls(values) -> int:
    return len(_dedupe_str_list(values))


def _should_refetch_mercari_detail_via_browser_pool(item: dict, meta: dict) -> bool:
    page_type = str((meta or {}).get("page_type") or "").strip().lower()
    status = str((item or {}).get("status") or "").strip().lower()
    if page_type == "deleted_detail" or status == "deleted":
        return False
    return _count_image_urls((item or {}).get("image_urls")) <= 1


def _score_mercari_dom_result(item: dict, meta: dict) -> int:
    page_type = str((meta or {}).get("page_type") or "").strip().lower()
    status = str((item or {}).get("status") or "").strip().lower()

    score = _count_image_urls((item or {}).get("image_urls")) * 10
    if page_type in {"active_detail", "sold_detail"}:
        score += 6
    elif page_type == "unknown_detail":
        score += 2

    if status in {"on_sale", "sold", "deleted"}:
        score += 4
    if _is_nonempty_text((item or {}).get("title")):
        score += 2
    if _is_positive_price((item or {}).get("price")):
        score += 2
    return score


def _maybe_refetch_mercari_detail_with_browser_pool(url: str, item: dict, meta: dict) -> tuple[dict, dict]:
    if not _should_refetch_mercari_detail_via_browser_pool(item, meta):
        return item, meta

    try:
        refetched_page, refetch_payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(
            url, network_idle=True,
        )
    except Exception as exc:
        logger.warning("Mercari browser-pool detail refetch failed for %s: %s", url, exc)
        return item, meta

    refetched_page = _normalize_mercari_detail_page(refetched_page, url)
    refetched_item, refetched_meta = parse_mercari_item_page(refetched_page, url)

    # Merge any payload images captured during the refetch
    if refetch_payloads:
        bp_result = _select_best_mercari_payload(refetch_payloads, url)
        bp_item = dict(bp_result.get("item") or {})
        if _is_nonempty_list(bp_item.get("image_urls")):
            merged_imgs = list(refetched_item.get("image_urls") or [])
            for img_url in bp_item["image_urls"]:
                _append_unique_image_url(merged_imgs, img_url)
            if len(merged_imgs) > len(refetched_item.get("image_urls") or []):
                refetched_item["image_urls"] = merged_imgs
                refetched_meta.setdefault("field_sources", {})
                old_src = refetched_meta["field_sources"].get("image_urls", "dom")
                refetched_meta["field_sources"]["image_urls"] = _merge_source_labels(old_src, "payload")

    if _score_mercari_dom_result(refetched_item, refetched_meta) <= _score_mercari_dom_result(item, meta):
        return item, meta

    merged_meta = dict(refetched_meta or {})
    merged_meta["dom_refetch"] = "browser_pool"
    return refetched_item, merged_meta


def _merge_mercari_results(payload_item: dict, payload_meta: dict, dom_item: dict, dom_meta: dict) -> tuple[dict, dict]:
    dom_meta = _build_mercari_dom_meta(dom_item, dom_meta)
    payload_meta = dict(payload_meta or {})
    payload_sources = dict(payload_meta.get("field_sources") or {})
    dom_sources = dict(dom_meta.get("field_sources") or {})

    merged = {
        "url": dom_item.get("url") or payload_item.get("url"),
        "title": "",
        "price": None,
        "status": dom_item.get("status") or payload_item.get("status") or "unknown",
        "description": "",
        "image_urls": [],
        "variants": [],
    }
    merged_sources = {}

    for field, validator in (
        ("title", _is_nonempty_text),
        ("price", _is_positive_price),
        ("description", _is_nonempty_text),
        ("variants", _is_nonempty_list),
        ("status", _is_useful_status),
    ):
        value, source = pick_first_valid(
            ("payload", payload_item.get(field)),
            ("dom", dom_item.get(field)),
            validator=validator,
            default=dom_item.get(field),
        )
        merged[field] = value
        if source == "payload":
            merged_sources[field] = payload_sources.get(field, "payload")
        elif source == "dom":
            merged_sources[field] = dom_sources.get(field, "dom")

    merged_images, merged_image_source = _merge_image_lists(
        payload_item.get("image_urls"),
        payload_sources.get("image_urls", "payload"),
        dom_item.get("image_urls"),
        dom_sources.get("image_urls", "dom"),
    )
    merged["image_urls"] = merged_images
    if merged_image_source:
        merged_sources["image_urls"] = merged_image_source

    merged_meta = dict(dom_meta)
    merged_meta["strategy"] = "payload" if any(source == "payload" for source in merged_sources.values()) else dom_meta.get("strategy", "dom")
    merged_meta["field_sources"] = merged_sources
    merged_meta["payload_merge"] = True
    attach_extraction_trace(merged, strategy=merged_meta["strategy"], field_sources=merged_sources)
    return merged, merged_meta


# Regex identifying a Mercari API response URL that explicitly names an
# item id, e.g. ``/items/get?id=m123`` or ``/items/m123``.  Returning the
# id from the URL lets us (a) skip responses that clearly name a
# *different* item and (b) boost the score of the canonical item endpoint
# even when its payload dict doesn't embed an ``id`` field.
_MERCARI_RESPONSE_ITEM_ID_PATTERN = re.compile(
    r"(?:[?&]id=|/items/)(m\d+)",
    re.IGNORECASE,
)


def _response_url_item_id(response_url: str) -> str:
    """Return the ``m\\d+`` item id encoded in a Mercari API response URL."""
    match = _MERCARI_RESPONSE_ITEM_ID_PATTERN.search(str(response_url or ""))
    return match.group(1).lower() if match else ""


def _select_best_mercari_payload(captured_payloads: list[dict], item_url: str) -> dict:
    """Pick the best payload-provided item dict for title/price/status/description.

    When we know the target ``m\\d+`` item id we only consider responses
    that either (a) embed a dict whose ``id`` matches the target or (b)
    come from a URL that explicitly names the target (e.g.
    ``/items/get?id=m123`` or ``/items/m123``).  Everything else could
    contain data from related / similar / seller-other items.

    Photos are **not** used to rank candidates here.  The final photo
    list is assembled by :func:`collect_mercari_photo_urls_for_item`
    from every captured blob so that we never lose photos because the
    highest-scoring dict happened to be sparse.
    """
    target_item_id = extract_mercari_item_id(item_url)
    best = {
        "item": {},
        "meta": {
            "strategy": "payload",
            "field_sources": {},
            "reasons": ["payload-candidate-missing"],
            "target_item_id": target_item_id,
        },
        "response_url": "",
        "responses_seen": len(captured_payloads),
        "raw_payload": None,
    }
    best_score = -1

    for response_payload in captured_payloads:
        response_url = str(response_payload.get("url", "") or "")
        response_item_id = _response_url_item_id(response_url)

        # Response URL explicitly names a *different* item — skip outright.
        if target_item_id and response_item_id and response_item_id != target_item_id:
            continue

        item, meta = parse_mercari_network_payload(
            response_payload.get("payload") or {},
            item_url,
        )
        candidate_id_matches_target = bool(meta.get("candidate_id_matches_target"))
        response_url_matches_target = bool(
            target_item_id and response_item_id == target_item_id
        )

        if target_item_id and not (candidate_id_matches_target or response_url_matches_target):
            # No positive evidence this response describes the requested item.
            continue

        score = sum(
            (
                4 if _is_nonempty_text(item.get("title")) else 0,
                3 if _is_positive_price(item.get("price")) else 0,
                2 if _is_useful_status(item.get("status")) else 0,
                1 if _is_nonempty_text(item.get("description")) else 0,
            )
        )
        if candidate_id_matches_target:
            score += 20
        elif response_url_matches_target:
            score += 10

        if score <= 0:
            continue
        if score > best_score:
            best_score = score
            best = {
                "item": item,
                "meta": meta,
                "response_url": response_url,
                "responses_seen": len(captured_payloads),
                "raw_payload": response_payload.get("payload"),
            }

    return best


def _merge_target_photo_url_union(
    item: dict,
    meta: dict,
    blobs,
    target_item_id: str,
) -> None:
    """Union every ``/photos/m<TARGET>_N.jpg`` URL found in ``blobs`` into ``item``.

    Mercari encodes the owning item id directly in photo paths, so URL-based
    matching is immune to cross-item contamination.  This acts as a final
    safety net: even when the winning payload candidate carried no (or only
    partial) photos, we still end up with every photo that was visible in
    any captured API response or the page HTML.
    """
    if not target_item_id:
        return
    photos = collect_mercari_photo_urls_for_item(blobs, target_item_id)
    if not photos:
        return
    existing = list(item.get("image_urls") or [])
    combined = list(photos)
    for existing_url in existing:
        if existing_url not in combined:
            combined.append(existing_url)
    if len(combined) <= len(existing):
        return
    item["image_urls"] = combined
    field_sources = dict((meta or {}).get("field_sources") or {})
    old_source = field_sources.get("image_urls")
    field_sources["image_urls"] = (
        _merge_source_labels(old_source, "payload-url-union")
        if old_source
        else "payload-url-union"
    )
    meta["field_sources"] = field_sources


async def _capture_mercari_network_payload_async(url: str) -> dict:
    captured_payloads = []
    response_tasks = []

    async def _task(page, context):
        async def _capture_response(response):
            try:
                headers = await response.all_headers()
                content_type = str(headers.get("content-type", "") or "").lower()
                if "json" not in content_type and not response.url.lower().endswith(".json"):
                    return
                if "mercari" not in response.url.lower():
                    return
                payload = await response.json()
                captured_payloads.append({"url": response.url, "payload": payload})
            except Exception:
                return

        page.on("response", lambda response: response_tasks.append(asyncio.create_task(_capture_response(response))))
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            await page.wait_for_selector("h1, [data-testid='price']", timeout=5000)
        except Exception:
            pass
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)

    await run_browser_page_task(
        "mercari",
        _task,
        headless=True,
        launch_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
        ],
        context_options={
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "extra_http_headers": {"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
        },
    )

    return _select_best_mercari_payload(captured_payloads, url)


def _capture_mercari_network_payload(url: str) -> dict:
    return run_coro_sync(_capture_mercari_network_payload_async(url))


def _extract_price_from_text(text: str):
    if not text:
        return None

    for pattern in (r"[¥￥]\s*([\d,]+)", r"([\d,]+)\s*円"):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            digits = re.sub(r"\D", "", match.group(1) or "")
            if not digits:
                continue
            return int(digits)
        except ValueError:
            continue

    return None


def _extract_plain_number_from_text(text: str):
    """
    価格セレクタから currency 記号なしで返ってくる数値を安全に抽出する。
    カンマだけを拾った場合は None を返す。
    """
    if not text:
        return None

    match = re.search(r"\d[\d,]*", text)
    if not match:
        return None

    digits = re.sub(r"\D", "", match.group(0) or "")
    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def _normalize_mercari_shops_title(text: str) -> str:
    if not text:
        return ""

    normalized = text.strip()
    normalized = re.sub(r"\s*[-|｜]\s*メルカリ(?:\s*Shops)?\s*$", "", normalized).strip()
    normalized = re.sub(r"\s*[-|｜]\s*Mercari(?:\s*Shops)?\s*$", "", normalized).strip()

    if normalized in {"メルカリ", "Mercari"}:
        return ""
    return normalized


def _extract_mercari_shops_title_from_body(body_text: str) -> str:
    if not body_text:
        return ""

    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    skip_lines = {"コンテンツにスキップ", "ログイン", "会員登録", "出品", "日本語", "メルカリShops", "質問する"}

    for index, line in enumerate(lines):
        if line in skip_lines:
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", line):
            continue
        if _extract_price_from_text(line) is not None or line == "¥":
            continue

        nearby_lines = lines[index + 1:index + 4]
        if any(candidate == "¥" or _extract_price_from_text(candidate) is not None for candidate in nearby_lines):
            return line

    return ""


def _infer_mercari_shops_status(body_text: str) -> str:
    if not body_text:
        return "unknown"

    purchase_markers = ("購入手続きへ", "購入する", "カートに入れる", "今すぐ購入", "Buy this item")
    sold_markers = ("この商品は売り切れです", "在庫なし", "現在在庫がありません")
    positive_stock_pattern = r"残り\s*\d+\s*点"

    if any(marker in body_text for marker in purchase_markers):
        return "on_sale"
    if re.search(positive_stock_pattern, body_text):
        return "on_sale"
    if any(marker in body_text for marker in sold_markers):
        return "sold"
    if "売り切れ" in body_text and "残り" not in body_text:
        return "sold"
    return "unknown"


async def _extract_first_non_empty_text_async(page, selectors: list) -> str:
    for selector in selectors:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue

        for element in elements:
            try:
                candidate = (await element.inner_text()).strip()
            except Exception:
                continue
            if candidate:
                return candidate

    return ""



async def _extract_shops_variants_async(page, label_texts: list) -> list:
    """
    メルカリShopsのバリエーションを Playwright で取得。
    """
    found_options = []
    
    for label_text in label_texts:
        # ラベルテキストを含む要素を XPath で検索
        labels = await page.query_selector_all(
            f"xpath=//*[contains(text(), '{label_text}')]"
        )
        
        for label in labels:
            try:
                tag_name = await label.evaluate("el => el.tagName.toLowerCase()")
                if tag_name in ['script', 'style']:
                    continue
                
                # 親要素の nextElementSibling（コンテナ）を取得
                container = await label.evaluate_handle(
                    "el => el.parentElement && el.parentElement.nextElementSibling"
                )
                
                # Check for truthful element handle (not None and not evaluating to null)
                is_valid = await container.evaluate("el => el !== null")
                if not is_valid:
                    continue
                
                # コンテナの直下の子要素を取得
                children = await container.query_selector_all(":scope > *")
                
                if children:
                    options = []
                    for child in children:
                        raw_text = await child.inner_text()
                        raw_text = raw_text.strip()
                        if not raw_text:
                            continue
                        
                        # 1行目のみ取得
                        val = raw_text.split('\n')[0].strip()
                        
                        # 価格・在庫情報を削除（正規表現クリーニング）
                        val = re.sub(r'[¥￥]\s*[\d,]+', '', val)
                        val = re.sub(r'[\d,]+\s*円', '', val)
                        val = re.sub(r'残り\d+点', '', val)
                        val = re.sub(r'売り切れ|在庫なし', '', val)
                        val = val.strip()
                        
                        # 不要なボタンを除外
                        if val and val not in ["いいね", "シェア", "もっと見る"]:
                            if val not in options:
                                options.append(val)
                    
                    if options:
                        found_options = options
                        break
                        
            except Exception:
                continue
        
        if found_options:
            break
    
    return found_options


async def _scrape_shops_product_async(url: str) -> dict:
    """メルカリShops商品ページを Playwright で取得"""
    async def _task(page, context):
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            try:
                await page.wait_for_selector("h1, [data-testid='product-price']", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)  # Shopsはロードが遅いことがあるため待機
            body_text = await page.evaluate("document.body.innerText")
            
            # ---- タイトル ----
            title_selectors = get_selectors('mercari', 'shops', 'title') or ["[data-testid='product-name']", "h1"]
            title = await _extract_first_non_empty_text_async(page, title_selectors)
            if not title:
                title = _normalize_mercari_shops_title(await page.title())
            if not title:
                title = _extract_mercari_shops_title_from_body(body_text)

            # ---- 価格 ----
            price = None
            price_selectors = get_selectors('mercari', 'shops', 'price') or ["[data-testid='product-price']"]
            for selector in price_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                except Exception:
                    continue
                if elements:
                    price_text = await elements[0].inner_text()
                    price = _extract_price_from_text(price_text)
                    if price is not None:
                        break
                        
            # 予備の価格取得ロジック
            if price is None:
                price = _extract_price_from_text(body_text)

            # ---- 説明文 ----
            desc_selectors = get_selectors('mercari', 'shops', 'description') or ["[data-testid='product-description']"]
            description = await _extract_first_non_empty_text_async(page, desc_selectors)

            # 説明文フォールバック: meta description
            if not description:
                meta = await page.query_selector("meta[name='description']")
                if meta:
                    description = await meta.get_attribute("content") or ""

            # 説明文フォールバック: body text から抽出
            if not description:
                body_text = await page.evaluate("document.body.innerText")
                if "商品の説明" in body_text:
                    after = body_text.split("商品の説明", 1)[1]
                    end_pos = len(after)
                    for marker in ["商品の情報", "ショップ情報", "おすすめ商品", "レビュー"]:
                        idx = after.find(marker)
                        if idx != -1 and idx < end_pos:
                            end_pos = idx
                    description = after[:end_pos].strip()[:500]

            # ---- 画像 ----
            image_urls = []
            image_selectors = get_selectors('mercari', 'shops', 'images') or ["img[src*='mercari'][src*='static']"]
            for selector in image_selectors:
                try:
                    imgs = await page.query_selector_all(selector)
                except Exception:
                    continue
                for img in imgs:
                    src = await img.get_attribute("src")
                    if src and src not in image_urls:
                        image_urls.append(src)

            # ---- バリエーション（簡易取得） ----
            variants = []
            item_data_update = {}
            
            colors = await _extract_shops_variants_async(page, ['カラー', 'Color'])
            types = await _extract_shops_variants_async(page, ['種類', 'サイズ', 'Size'])
            logger.debug("Mercari Shops variants: colors=%s types=%s", colors, types)

            # 色と種類を組み合わせてバリエーションを作成
            if colors and types:
                option1_name = "カラー"
                option2_name = "サイズ/種類"
                for color in colors:
                    for type_val in types:
                        variants.append({
                            "option1_name": option1_name,
                            "option1_value": color,
                            "option2_name": option2_name,
                            "option2_value": type_val,
                            "price": price,
                            "inventory_qty": 1 
                        })
                item_data_update = {"option1_name": option1_name, "option2_name": option2_name}
                
            elif colors: # 色だけの場合
                option1_name = "カラー"
                for color in colors:
                    variants.append({
                        "option1_name": option1_name,
                        "option1_value": color,
                        "price": price,
                        "inventory_qty": 1
                    })
                item_data_update = {"option1_name": option1_name}

            elif types: # 種類だけの場合
                option1_name = "種類" # デフォルト
                for type_val in types:
                    variants.append({
                        "option1_name": option1_name,
                        "option1_value": type_val,
                        "price": price,
                        "inventory_qty": 1
                    })
                item_data_update = {"option1_name": option1_name}

            # ---- ステータス ----
            status = _infer_mercari_shops_status(body_text)
            default_inventory_qty = 1 if status == "on_sale" else 0
            for variant in variants:
                variant["inventory_qty"] = default_inventory_qty

            # Update item_data with found option names
            item_data = {
                "url": url,
                "title": title,
                "price": price,
                "status": status,
                "description": description,
                "image_urls": image_urls,
                "variants": variants
            }
            item_data.update(item_data_update)
            item_data["_scrape_meta"] = {
                "strategy": "browser",
                "page_type": "shops_detail",
                "confidence": "high",
                "field_sources": {},
                "reasons": [],
            }
            return item_data

    return await run_browser_page_task(
        "mercari",
        _task,
        headless=True,
        launch_args=["--no-sandbox", "--disable-dev-shm-usage"],
    )


def scrape_shops_product(url: str, driver=None) -> dict:
    """メルカリShops商品ページ用スクレイピング（同期ラッパー）"""
    item = run_coro_sync(_scrape_shops_product_async(url))
    meta = dict(item.get("_scrape_meta") or {})
    report_detail_result("mercari", url, item, meta, page_type="shops_detail")
    return item



def scrape_item_detail(url: str, driver=None):
    """1つの商品ページから詳細情報を取得して dict で返す"""

    # Shops URL判定
    if "/shops/product/" in url:
        return scrape_shops_product(url)

    capture_enabled = _should_capture_mercari_network_payload()
    use_payload = _should_use_mercari_network_payload()
    payload_result = {}
    payload_item = {}
    payload_meta = {}
    browser_pool_payloads = []
    network_capture = {
        "enabled": capture_enabled,
        "captured": False,
        "responses_seen": 0,
        "response_url": "",
        "used_payload": False,
    }
    used_browser_pool_detail = should_use_mercari_browser_pool_detail()

    if capture_enabled and not used_browser_pool_detail:
        try:
            payload_result = _capture_mercari_network_payload(url) or {}
            payload_item = dict(payload_result.get("item") or {})
            payload_meta = dict(payload_result.get("meta") or {})
            network_capture["responses_seen"] = int(payload_result.get("responses_seen") or 0)
            network_capture["response_url"] = str(payload_result.get("response_url") or "")
            network_capture["captured"] = _has_usable_payload_item(payload_item)
        except Exception as exc:
            network_capture["capture_error"] = str(exc)
            logger.warning("Mercari payload capture failed for %s: %s", url, exc)

    try:
        if used_browser_pool_detail:
            # Use combined page + payload fetcher so we get both DOM
            # and network payloads (including all image URLs) in a
            # single browser session with carousel interaction.
            page, browser_pool_payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(
                url, network_idle=True,
            )
            if capture_enabled:
                payload_result = _select_best_mercari_payload(browser_pool_payloads, url)
                payload_item = dict(payload_result.get("item") or {})
                payload_meta = dict(payload_result.get("meta") or {})
                network_capture["responses_seen"] = int(payload_result.get("responses_seen") or 0)
                network_capture["response_url"] = str(payload_result.get("response_url") or "")
                network_capture["captured"] = _has_usable_payload_item(payload_item)
        else:
            page = fetch_dynamic(url, headless=True, network_idle=True)
    except Exception as e:
        if use_payload and _has_usable_payload_item(payload_item):
            payload_item = dict(payload_item)
            payload_meta = dict(payload_meta)
            payload_item.setdefault("url", url)
            payload_meta.setdefault("strategy", "payload")
            payload_meta.setdefault("field_sources", dict(payload_meta.get("field_sources") or {}))
            payload_meta["network_capture"] = network_capture
            payload_meta["fallback_mode"] = "payload_without_dom"
            attach_extraction_trace(
                payload_item,
                strategy=payload_meta.get("strategy", "payload"),
                field_sources=payload_meta.get("field_sources") or {},
            )
            payload_item["_scrape_meta"] = payload_meta
            network_capture["used_payload"] = True
            logger.warning("Mercari DOM fetch failed for %s; returning captured payload result", url)
            return payload_item
        print(f"Error accessing {url}: {e}")
        return {
            "url": url, "title": "", "price": None, "status": "error",
            "description": "", "image_urls": [], "variants": []
        }
    page = _normalize_mercari_detail_page(page, url)
    dom_item, dom_meta = parse_mercari_item_page(page, url)
    if not used_browser_pool_detail:
        dom_item, dom_meta = _maybe_refetch_mercari_detail_with_browser_pool(url, dom_item, dom_meta)

    # When browser pool captured network payloads, merge their images
    # into dom_item so all available images are included even if the
    # DOM carousel only rendered a subset.
    if browser_pool_payloads and not _has_usable_payload_item(payload_item):
        bp_result = _select_best_mercari_payload(browser_pool_payloads, url)
        bp_item = dict(bp_result.get("item") or {})
        bp_meta = dict(bp_result.get("meta") or {})
        if _has_usable_payload_item(bp_item):
            payload_item = bp_item
            payload_meta = bp_meta
            network_capture["responses_seen"] = max(
                network_capture["responses_seen"],
                int(bp_result.get("responses_seen") or 0),
            )
            network_capture["captured"] = True

    has_usable_payload_item = _has_usable_payload_item(payload_item)
    # Keep shadow-compare enabled whenever we have a usable payload item,
    # including payloads sourced from browser-pool detail fetch.
    shadow_compare = _build_shadow_compare(payload_item, dom_item) if capture_enabled or has_usable_payload_item else {}
    if shadow_compare.get("mismatch_fields"):
        logger.info(
            "Mercari payload/dom mismatch for %s: %s",
            url,
            ",".join(shadow_compare["mismatch_fields"]),
        )

    if (use_payload or bool(browser_pool_payloads)) and has_usable_payload_item:
        item, meta = _merge_mercari_results(payload_item, payload_meta, dom_item, dom_meta)
        network_capture["used_payload"] = True
    else:
        item, meta = dom_item, dict(dom_meta or {})

    meta = dict(meta or {})
    meta["network_capture"] = network_capture
    if shadow_compare:
        meta["shadow_compare"] = shadow_compare

    # Final photo safety net: regardless of which payload candidate (or the
    # DOM) supplied ``image_urls``, union in every /photos/m<TARGET>_N.jpg
    # URL we saw in any captured response or the page HTML.  Mercari encodes
    # the owning item id in photo paths so this can only add photos that
    # truly belong to the target item — it is immune to cross-item
    # contamination.
    target_item_id = extract_mercari_item_id(url)
    if target_item_id:
        blobs: list = []
        for captured in browser_pool_payloads or []:
            if isinstance(captured, dict):
                blobs.append(captured.get("payload"))
        if page is not None:
            blobs.append(getattr(page, "body", "") or "")
        _merge_target_photo_url_union(item, meta, blobs, target_item_id)

    item["_scrape_meta"] = meta
    report_detail_result("mercari", url, item, meta, page_type="detail")
    return item


async def _scrape_search_async(
    search_url: str,
    max_items: int,
    max_scroll: int,
) -> list[str]:
    """ページスクロールしながら商品リンクを収集する非同期関数"""
    async def _task(page, context):
            print(f"DEBUG: Navigating to {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            try:
                # Wait up to 10 seconds for at least one item link to render
                await page.wait_for_selector("a[data-testid='thumbnail-link'], a[href*='/item/']", timeout=10000)
            except Exception:
                print("DEBUG: Timeout waiting for item links. The page might be blocked or empty.")
            await page.wait_for_timeout(1000) # 追加待機
            
            item_urls = []
            seen_urls = set()
            
            for scroll_count in range(max_scroll * 2):
                # 現在表示されているリンクを収集（/item/ あるいはテストデータ等）
                links = await page.query_selector_all("a[data-testid='thumbnail-link']")
                if not links:
                    links = await page.query_selector_all("a[href*='/item/']")
                if not links:
                    links = await page.query_selector_all("li[data-testid='item-cell'] a")
                    
                for link in links:
                    href = await link.get_attribute("href")
                    if href and "/item/" in href:
                        # 絶対URLに変換
                        if href.startswith("/"):
                            href = f"https://jp.mercari.com{href}"
                        if href not in seen_urls:
                            seen_urls.add(href)
                            item_urls.append(href)
                
                if len(item_urls) >= max_items * 2:
                    break
                
                # ページを下にスクロール
                prev_count = len(item_urls)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)  # 2秒待機（新アイテム読み込み）
                
                # スクロール後に新しいリンクが増えていない場合は終了
                links_after = await page.query_selector_all("a[data-testid='thumbnail-link']")
                if not links_after:
                    links_after = await page.query_selector_all("a[href*='/item/']")
                if not links_after:
                     links_after = await page.query_selector_all("li[data-testid='item-cell'] a")
                if len(links_after) <= len(links):
                    # 成長していなければストップ
                    break
            
            print(f"DEBUG: Found {len(item_urls)} valid item URLs.")
            return item_urls[:max_items * 2]  # 最大 max_items * 2 件

    return await run_browser_page_task(
        "mercari",
        _task,
        headless=True,
        launch_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
        ],
        context_options={
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "extra_http_headers": {
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            },
        },
        init_scripts=[
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """
        ],
    )


async def _scrape_item_detail_async(url: str) -> dict:
    return await asyncio.to_thread(scrape_item_detail, url)


async def _collect_search_items_async(item_urls: list[str], max_items: int) -> list[dict]:
    settings = get_async_fetch_settings("mercari")
    candidate_urls = item_urls[: max_items * 2]
    detail_results = await gather_with_concurrency(
        candidate_urls,
        _scrape_item_detail_async,
        concurrency=settings.concurrency,
    )

    filtered_items = []
    for url, data in zip(candidate_urls, detail_results):
        if len(filtered_items) >= max_items:
            break

        print(f"DEBUG: Scraping item {url}")
        if isinstance(data, Exception):
            print(f"DEBUG: Error scraping {url}: {data}")
            continue
        if data and data.get("title") and data.get("status") != "error":
            print(f"DEBUG: Success -> {data['title']}")
            filtered_items.append(data)
        else:
            print("DEBUG: Failed to get valid data (empty title or error)")

    return filtered_items


def scrape_search_result(
    search_url: str,
    max_items: int = 5,
    max_scroll: int = 3,
    headless: bool = True,
):
    """
    メルカリ検索URLから複数商品をスクレイピングして list[dict] を返す。
    Playwright を直接使用してページスクロール取得を実現。
    """
    try:
        item_urls = run_coro_sync(
            _scrape_search_async(search_url, max_items, max_scroll)
        )
    except Exception as e:
        logging.error(f"Search scrape failed: {e}")
        return []
    
    return run_coro_sync(_collect_search_items_async(item_urls, max_items))


def scrape_single_item(url: str, headless: bool = True):
    """
    指定された商品URLを1件だけスクレイピングして list[dict] を返す。
    save_scraped_items_to_db にそのまま渡せるようにリストに包んでいる。
    """
    metrics = get_metrics()
    metrics.start('mercari', 'single')
    try:
        logger.debug("Starting scrape_single_item for %s", url)
        
        data = scrape_item_detail(url)
        log_scrape_result('mercari', url, data)
        
        if data.get("title"):
            logger.debug("Mercari scrape success: %s", data["title"])
        else:
            logger.debug("Mercari scrape failed to get title for %s", url)

        metrics.finish()
        return [data]

    except Exception as e:
        print(f"CRITICAL ERROR during single scraping: {e}")
        import traceback
        traceback.print_exc()
        metrics.record_attempt(False, url, str(e))
        metrics.finish()
        return []
