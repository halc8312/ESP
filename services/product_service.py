"""
Product service for database operations related to scraped items.
"""
import hashlib
import logging
import os

from flask import has_request_context, session

from database import SessionLocal
from models import Product, ProductSnapshot, Shop, Variant
from services.image_service import split_image_url_string
from services.pricing_service import product_has_pricing_config, update_product_selling_price
from services.scrape_result_policy import (
    evaluate_persistence,
    normalize_item_for_persistence,
    normalize_price_for_persistence,
)
from time_utils import utc_now
from utils import normalize_url


logger = logging.getLogger(__name__)

_IMAGE_CACHE_ENABLED = (
    os.environ.get("SCRAPE_IMAGE_CACHE_ENABLED", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
_SHOP_ID_UNSET = object()


def _empty_save_summary(input_count: int = 0):
    return {
        "input_count": input_count,
        "processed_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "rejected_count": 0,
    }


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_image_urls(value) -> list[str]:
    if isinstance(value, str):
        candidates = split_image_url_string(value.replace("\r", "|").replace("\n", "|"))
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = []

    normalized_urls = []
    seen_urls = set()
    for candidate in candidates:
        url = _normalize_text(candidate)
        if not url:
            continue
        if url in seen_urls:
            continue
        normalized_urls.append(url)
        seen_urls.add(url)
    return normalized_urls


def _normalize_non_negative_int(value, default: int = 0) -> int:
    normalized = normalize_price_for_persistence(value)
    if normalized is None or normalized < 0:
        return default
    return normalized


def _resolve_owned_shop_id(session_db, *, user_id: int, raw_shop_id):
    """Resolve a shop only when it belongs to the product owner.

    Scrape jobs can outlive the browser session that created them.  Treat a
    stale/malformed queued shop context as unscoped instead of ever attaching
    the resulting product to another user's shop.
    """
    if raw_shop_id in (None, ""):
        return None

    if isinstance(raw_shop_id, bool):
        logger.warning("Ignoring invalid shop context for user_id=%s", user_id)
        return None
    try:
        shop_id = int(raw_shop_id)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid shop context for user_id=%s", user_id)
        return None

    if shop_id <= 0:
        logger.warning("Ignoring invalid shop context for user_id=%s", user_id)
        return None

    owned_shop = session_db.query(Shop.id).filter(
        Shop.id == shop_id,
        Shop.user_id == user_id,
    ).first()
    if owned_shop is None:
        logger.warning(
            "Ignoring non-owned shop context shop_id=%s user_id=%s",
            shop_id,
            user_id,
        )
        return None
    return shop_id


def _default_inventory_for_status(status: str) -> int:
    return 1 if status == "on_sale" else 0


def _normalize_scraped_variants(raw_variants, *, fallback_price, status: str):
    if not isinstance(raw_variants, (list, tuple)):
        return []

    normalized_variants = []
    for index, variant in enumerate(raw_variants, 1):
        if not isinstance(variant, dict):
            continue

        variant_price = normalize_price_for_persistence(variant.get("price"))
        if variant_price is None:
            variant_price = fallback_price

        inventory_default = _default_inventory_for_status(status)
        inventory_qty = _normalize_non_negative_int(variant.get("inventory_qty"), inventory_default)
        if status in {"sold", "deleted"}:
            inventory_qty = 0

        normalized_variants.append(
            {
                "option1_name": _normalize_text(variant.get("option1_name")),
                "option1_value": _normalize_text(variant.get("option1_value")) or f"Option {index}",
                "option2_name": _normalize_text(variant.get("option2_name")),
                "option2_value": _normalize_text(variant.get("option2_value")) or None,
                "option3_name": _normalize_text(variant.get("option3_name")),
                "option3_value": _normalize_text(variant.get("option3_value")) or None,
                "price": variant_price,
                "inventory_qty": inventory_qty,
            }
        )
    return normalized_variants


def _find_product_for_source(session_db, *, url: str, user_id: int, shop_id):
    base_query = session_db.query(Product).filter_by(source_url=url, user_id=user_id)

    if shop_id is None:
        return base_query.filter(Product.shop_id.is_(None)).order_by(Product.id.asc()).first()

    scoped_product = (
        base_query
        .filter(Product.shop_id == shop_id)
        .order_by(Product.id.asc())
        .first()
    )
    if scoped_product is not None:
        return scoped_product

    return (
        base_query
        .filter(Product.shop_id.is_(None))
        .order_by(Product.id.asc())
        .first()
    )


def _cache_external_images(
    image_urls: list[str],
    product_id: int,
) -> list[str]:
    """Try to download external images and return local ``/media/`` URLs.

    For each external URL, attempt to cache it locally via
    :func:`~services.image_service.cache_product_image`. If caching
    succeeds the local URL replaces the external one; otherwise the
    original URL is kept so the product still displays the image via the
    browser (which can often reach CDNs that the server cannot).
    """
    if not _IMAGE_CACHE_ENABLED:
        return image_urls

    from services.image_service import cache_product_image

    result: list[str] = []
    for idx, url in enumerate(image_urls):
        if not url.startswith(("http://", "https://")):
            result.append(url)
            continue
        local_url = cache_product_image(url, product_id, idx)
        result.append(local_url if local_url else url)
    return result


def save_scraped_items_to_db(
    items,
    user_id: int,
    site: str = "mercari",
    shop_id=_SHOP_ID_UNSET,
    manual_selection: bool = False,
    return_summary: bool = False,
    raise_on_error: bool = False,
    is_listed: bool = True,
):
    """
    mercari_db.scrape_search_result() が返した items(list[dict]) を
    Product / ProductSnapshot に保存する。
    """
    input_count = len(items or [])
    if not items:
        summary = _empty_save_summary(input_count)
        return summary if return_summary else (0, 0)

    session_db = SessionLocal()
    new_count = 0
    updated_count = 0
    processed_count = 0
    rejected_count = 0
    now = utc_now()
    repricing_product_ids = set()
    saved_product_ids: list[int] = []
    completion_translation_ids: list[str] = []

    try:
        shop_id_from_session = False
        raw_shop_id = shop_id
        if raw_shop_id is _SHOP_ID_UNSET and has_request_context():
            raw_shop_id = session.get("current_shop_id")
            shop_id_from_session = raw_shop_id is not None
        elif raw_shop_id is _SHOP_ID_UNSET:
            raw_shop_id = None

        resolved_shop_id = _resolve_owned_shop_id(
            session_db,
            user_id=user_id,
            raw_shop_id=raw_shop_id,
        )
        if shop_id_from_session and raw_shop_id is not None and resolved_shop_id is None:
            session.pop("current_shop_id", None)

        for item in items:
            if item.get("_listing_card") is True:
                product, created = _save_listing_card(
                    session_db, item, user_id=user_id, site=site,
                    shop_id=resolved_shop_id, is_listed=is_listed,
                )
                if product is None:
                    rejected_count += 1
                    continue
                processed_count += 1
                new_count += int(created)
                if product.id not in saved_product_ids:
                    saved_product_ids.append(product.id)
                continue
            raw_url = item.get("url", "")
            if not raw_url:
                rejected_count += 1
                continue

            url = normalize_url(raw_url)
            normalized_item = normalize_item_for_persistence(item, manual_selection=manual_selection)
            scrape_meta = item.get("_scrape_meta") or {}

            title = _normalize_text(normalized_item.get("title"))
            price = normalized_item.get("price")
            status = normalized_item.get("status") or ""
            description = _normalize_text(normalized_item.get("description"))
            image_urls = _normalize_image_urls(normalized_item.get("image_urls"))
            image_urls_str = "|".join(image_urls)

            product = _find_product_for_source(
                session_db,
                url=url,
                user_id=user_id,
                shop_id=resolved_shop_id,
            )
            if product is not None and product.detail_fetch_state not in (None, "complete"):
                # Preserve the permissive legacy manual-selection contract,
                # while requiring real detail evidence for shallow products.
                normalized_item = normalize_item_for_persistence(item, manual_selection=False)
                status = normalized_item.get("status") or "unknown"
                if evaluate_persistence(site, normalized_item, scrape_meta, product, manual_selection=False) == "reject":
                    rejected_count += 1
                    continue
            persistence_action = evaluate_persistence(
                site,
                normalized_item,
                scrape_meta,
                product,
                manual_selection=manual_selection,
            )
            if persistence_action == "reject":
                rejected_count += 1
                continue

            if product is None:
                if persistence_action != "allow_full":
                    rejected_count += 1
                    continue

                sku_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:10].upper()
                generated_sku = f"MER-{sku_hash}"

                product = Product(
                    user_id=user_id,
                    site=site,
                    shop_id=resolved_shop_id,
                    source_url=url,
                    last_title=title,
                    last_price=price,
                    last_status=status,
                    is_listed=is_listed,
                    created_at=now,
                    updated_at=now,
                )
                session_db.add(product)
                session_db.flush()
                new_count += 1
                processed_count += 1

                scraped_variants = _normalize_scraped_variants(
                    normalized_item.get("variants"),
                    fallback_price=price,
                    status=status,
                )
                if scraped_variants:
                    product.option1_name = (
                        _normalize_text(normalized_item.get("option1_name"))
                        or scraped_variants[0].get("option1_name")
                        or "Variation"
                    )
                    product.option2_name = (
                        _normalize_text(normalized_item.get("option2_name"))
                        or scraped_variants[0].get("option2_name")
                    )
                    product.option3_name = (
                        _normalize_text(normalized_item.get("option3_name"))
                        or scraped_variants[0].get("option3_name")
                    )

                    for i, v_data in enumerate(scraped_variants, 1):
                        new_variant = Variant(
                            product_id=product.id,
                            option1_value=v_data.get("option1_value") or f"Option {i}",
                            option2_value=v_data.get("option2_value"),
                            option3_value=v_data.get("option3_value"),
                            sku=f"{generated_sku}-{i}",
                            price=v_data.get("price", price),
                            taxable=False,
                            inventory_qty=v_data.get("inventory_qty", _default_inventory_for_status(status)),
                            position=i,
                        )
                        session_db.add(new_variant)
                else:
                    default_variant = Variant(
                        product_id=product.id,
                        option1_value="Default Title",
                        sku=generated_sku,
                        price=price,
                        taxable=False,
                        inventory_qty=_default_inventory_for_status(status),
                        position=1,
                    )
                    session_db.add(default_variant)

            else:
                if product.shop_id is None and resolved_shop_id is not None:
                    product.shop_id = resolved_shop_id

                if manual_selection and is_listed and product.is_listed is False:
                    product.is_listed = True

                if persistence_action == "allow_status_only":
                    status_changed = bool(status) and product.last_status != status
                    product.last_status = status or product.last_status
                    product.updated_at = now
                    existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                    if status in {"sold", "deleted"}:
                        for existing_variant in existing_variants:
                            existing_variant.inventory_qty = 0
                    if status_changed:
                        updated_count += 1
                    processed_count += 1
                    saved_product_ids.append(product.id)
                    translation_id = _complete_deferred_if_verified(session_db, product, item, site=site)
                    if translation_id:
                        completion_translation_ids.append(translation_id)
                    continue

                title_changed = bool(title.strip()) and product.last_title != title
                price_changed = price is not None and product.last_price != price
                status_changed = bool(status) and product.last_status != status

                if title.strip():
                    product.last_title = title
                if price is not None:
                    product.last_price = price
                if status:
                    product.last_status = status
                product.updated_at = now

                if title_changed or price_changed or status_changed:
                    updated_count += 1
                if price_changed and product_has_pricing_config(product):
                    repricing_product_ids.add(product.id)

                existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                for existing_variant in existing_variants:
                    if price is not None and (existing_variant.option1_value == "Default Title" or len(existing_variants) == 1):
                        existing_variant.price = price
                    if status in {"sold", "deleted"}:
                        existing_variant.inventory_qty = 0
                    elif status == "on_sale" and existing_variant.option1_value == "Default Title":
                        existing_variant.inventory_qty = existing_variant.inventory_qty or 1
                processed_count += 1

            if product.id not in saved_product_ids:
                saved_product_ids.append(product.id)

            cached_image_urls = _cache_external_images(
                image_urls, product.id
            )
            snapshot = ProductSnapshot(
                product_id=product.id,
                scraped_at=now,
                title=title,
                price=price,
                status=status,
                description=description,
                image_urls="|".join(cached_image_urls),
            )
            session_db.add(snapshot)
            translation_id = _complete_deferred_if_verified(session_db, product, item, site=site)
            if translation_id:
                completion_translation_ids.append(translation_id)

        for product_id in repricing_product_ids:
            update_product_selling_price(product_id, session=session_db)

        from services.scrape_job_runtime import assert_current_job_active

        assert_current_job_active()
        session_db.commit()

        if any(item.get("_listing_card") is True for item in items):
            from services.product_thumbnail_jobs import enqueue_thumbnail_jobs

            try:
                enqueue_thumbnail_jobs(saved_product_ids, user_id)
            except Exception as exc:
                # Pending demand is durable and the worker's existing recovery
                # scheduler refills it if the immediate queue call is unavailable.
                logger.warning("Thumbnail queue unavailable error_type=%s", type(exc).__name__)

        if completion_translation_ids:
            from services.product_detail_jobs import _dispatch_translation

            for translation_id in completion_translation_ids:
                _dispatch_translation(translation_id)

        summary = {
            "input_count": input_count,
            "processed_count": processed_count,
            "new_count": new_count,
            "updated_count": updated_count,
            "rejected_count": rejected_count,
            "product_ids": saved_product_ids,
        }
        return summary if return_summary else (new_count, updated_count)
    except Exception:
        session_db.rollback()
        logger.exception("DB 保存エラー")
        if raise_on_error:
            raise
        summary = _empty_save_summary(input_count)
        return summary if return_summary else (0, 0)
    finally:
        session_db.close()


def _complete_deferred_if_verified(session_db, product, item, *, site):
    """A normal verified detail registration supersedes an older lazy request."""
    if product.detail_fetch_state in (None, "complete"):
        return
    from services.search_result_quality import search_item_identity

    identity = search_item_identity(item, site=site)
    if identity is None or identity != search_item_identity({"url": product.source_url}, site=site):
        return
    meta = item.get("_scrape_meta") or {}
    if str(meta.get("confidence") or "high").lower() == "low":
        return
    if evaluate_persistence(site, item, meta, product, manual_selection=False) == "reject":
        return
    product.detail_fetch_state = "complete"
    # Any already queued worker now fails its token check before writing.
    product.detail_job_id = None
    product.detail_lease_expires_at = None
    product.detail_retry_at = None
    product.detail_fail_count = 0
    product.detail_error_code = None
    translation_id = None
    if product.detail_translate_requested:
        from services.product_detail_jobs import _create_completion_translation

        translation_id = _create_completion_translation(session_db, product)
        product.detail_translate_requested = False
    return translation_id


def _save_listing_card(session_db, item, *, user_id, site, shop_id, is_listed):
    """Persist a validated shallow card without replacing any older details."""
    from services.listing_cards import validate_listing_card

    validated = validate_listing_card(item, site=site)
    if not validated:
        return None, False
    url = normalize_url(item["url"])
    product = _find_product_for_source(
        session_db, url=url, user_id=user_id, shop_id=shop_id,
    )
    if product is None and site == "recordcity":
        # Language/host aliases share one validated catalog ID. Keep the
        # existing owner/shop lookup rule, including only the unscoped fallback.
        from services.search_result_quality import search_item_identity

        identity = search_item_identity(item, site=site)
        source_id = item["source_id"]
        candidates = session_db.query(Product).filter(
            Product.user_id == user_id, Product.site == site,
            Product.source_url.like(f"%/catalog/{source_id}%"),
            Product.shop_id.is_(None) if shop_id is None else (
                (Product.shop_id == shop_id) | Product.shop_id.is_(None)
            ),
        ).order_by(Product.id).all()
        matching = [candidate for candidate in candidates if search_item_identity({"url": candidate.source_url}, site=site) == identity]
        product = next((candidate for candidate in matching if candidate.shop_id == shop_id), matching[0] if matching else None)
    if product is not None:
        if product.deleted_at is not None:
            return None, False
        if product.shop_id is None and shop_id is not None:
            product.shop_id = shop_id
        if is_listed:
            product.is_listed = True
        # Even a pending card may now contain a verified sold observation or
        # manually edited inventory. Re-importing a list never changes it.
        if product.detail_fetch_state in ("pending", "queued", "running", "failed"):
            latest = session_db.query(ProductSnapshot).filter(
                ProductSnapshot.product_id == product.id,
            ).order_by(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc()).first()
            image = _normalize_image_urls(item.get("image_urls"))[:1]
            if latest is not None and image and latest.image_urls == image[0]:
                from services.product_thumbnail_jobs import create_thumbnail_demand

                # Explicit selection may rebind a shop, while only the same
                # validated card image can create/rearm an older shallow row.
                create_thumbnail_demand(session_db, product, latest)
        return product, False

    normalized = normalize_item_for_persistence(item)
    status = normalized["status"]
    now = utc_now()
    product = Product(
        user_id=user_id, shop_id=shop_id, site=site, source_url=url,
        last_title=_normalize_text(normalized["title"]),
        last_price=normalized["price"], last_status=status,
        is_listed=is_listed, created_at=now, updated_at=now,
        detail_fetch_state="pending", detail_fail_count=0,
    )
    session_db.add(product)
    session_db.flush()
    sku_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:10].upper()
    session_db.add(Variant(
        product_id=product.id, option1_value="Default Title",
        sku=f"MER-{sku_hash}", price=product.last_price,
        inventory_qty=_default_inventory_for_status(status), taxable=False, position=1,
    ))
    # Keep the source image internal without 300–500 synchronous downloads.
    # Public views use their placeholder until the durable thumbnail job
    # delivers the first image separately on the web service's disk.
    images = _normalize_image_urls(normalized["image_urls"])[:1]
    snapshot = ProductSnapshot(
        product_id=product.id, scraped_at=now, title=product.last_title,
        price=product.last_price, status=status, description="",
        image_urls="|".join(images),
    )
    session_db.add(snapshot)
    session_db.flush()
    from services.product_thumbnail_jobs import create_thumbnail_demand

    create_thumbnail_demand(session_db, product, snapshot)
    return product, True


def cache_deferred_detail_images(item, product_id, job_id):
    """Deliver a bounded set to the web disk before the final product write.

    Deferred snapshots contain only managed images. An unavailable image may
    leave a placeholder, while verified price/stock details still complete.
    """
    from services.bg_remover.image_fetch import build_image_fetch_headers
    from services.image_service import ImageValidationError, _response_status, download_external_image
    from services.marketplace_access import marketplace_access, observe_access_response
    from services.product_image_delivery import deliver_image_bytes
    from services.scrape_job_runtime import assert_current_job_active

    urls = _normalize_image_urls(item.get("image_urls"))[:8]
    if not _IMAGE_CACHE_ENABLED:
        return []
    cached = []
    for index, url in enumerate(urls):
        assert_current_job_active()
        if not url.startswith(("http://", "https://")):
            # A marketplace page cannot choose another owner's media path.
            continue
        try:
            # CDN requests share the owning marketplace's pacing and budget;
            # exact image-host/DNS/redirect validation stays in image_service.
            source_url = item.get("url")
            with marketplace_access(source_url, timeout_seconds=30, consume_request=False) as parent:
                def admit_hop(_current_url):
                    return marketplace_access(source_url, timeout_seconds=30, parent_lease=parent)

                def observe_hop(_current_url, response):
                    observe_access_response(source_url, _response_status(response), headers={"Retry-After": response.headers.get("Retry-After")})

                try:
                    content, _ext = download_external_image(
                        url, headers=build_image_fetch_headers(url),
                        request_admission=admit_hop, response_observer=observe_hop,
                    )
                except ImageValidationError as exc:
                    status = getattr(exc, "status_code", None)
                    if status in (403, 429):
                        observe_access_response(source_url, status, headers=getattr(exc, "response_headers", None))
                    raise
            cached.append(deliver_image_bytes(product_id, job_id, index, content))
        except Exception as exc:
            logger.warning("Deferred image delivery failed product_id=%s error_type=%s", product_id, type(exc).__name__)
    assert_current_job_active()
    return cached


def save_scraped_product_detail(session_db, product, item, *, cached_images=None):
    """Apply one verified detail to the exact locked Product in caller's transaction.

    Reject ambiguous data before writing. The caller fences the request token
    and commits this update together with its terminal detail state.
    """
    from services.search_result_quality import search_item_identity

    normalized = normalize_item_for_persistence(item)
    source_identity = search_item_identity({"url": product.source_url}, site=product.site)
    result_identity = search_item_identity(item, site=product.site)
    if source_identity is None or result_identity != source_identity:
        raise ValueError("detail_identity_mismatch")
    action = evaluate_persistence(product.site, normalized, item.get("_scrape_meta"), product)
    if action == "reject":
        raise ValueError("detail_unverified")
    now = utc_now()
    status = normalized["status"]
    if action == "allow_status_only":
        product.last_status = status
        if status in {"sold", "deleted"}:
            for variant in product.variants:
                variant.inventory_qty = 0
        product.updated_at = now
        return

    price = normalized["price"]
    title = _normalize_text(normalized["title"])
    product.last_title = title
    product.last_price = price
    product.last_status = status
    product.updated_at = now
    parsed_variants = _normalize_scraped_variants(
        normalized.get("variants"), fallback_price=price, status=status,
    )
    # The initial listing default variant has no detail options. Replace it
    # only on the first successful completion, preserving any chosen sale price.
    if parsed_variants and len(product.variants) == 1 and product.variants[0].option1_value == "Default Title":
        old = product.variants[0]
        sale_override = old.selling_price
        product.variants.remove(old)
        for index, raw in enumerate(parsed_variants, 1):
            product.variants.append(Variant(
                option1_value=raw["option1_value"], option2_value=raw["option2_value"],
                option3_value=raw["option3_value"], price=raw["price"],
                inventory_qty=raw["inventory_qty"], position=index,
                taxable=False, selling_price=sale_override,
                sku=f"MER-{hashlib.md5(product.source_url.encode('utf-8')).hexdigest()[:10].upper()}-{index}",
            ))
        for number in (1, 2, 3):
            key = f"option{number}_name"
            setattr(product, key, _normalize_text(normalized.get(key)) or parsed_variants[0][key])
    else:
        for variant in product.variants:
            if len(product.variants) == 1 or variant.option1_value == "Default Title":
                variant.price = price
            if status in {"sold", "deleted"}:
                variant.inventory_qty = 0
            elif status == "on_sale" and variant.option1_value == "Default Title":
                variant.inventory_qty = variant.inventory_qty or 1
    images = list(cached_images) if cached_images is not None else _cache_external_images(_normalize_image_urls(normalized.get("image_urls")), product.id)
    if not images:
        previous = session_db.query(ProductSnapshot).filter_by(product_id=product.id).order_by(
            ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc(),
        ).first()
        images = _normalize_image_urls(previous.image_urls) if previous else []
    if cached_images is not None:
        # The shallow listing snapshot may still retain its private CDN URL.
        # A verified deferred snapshot accepts only images hosted by the web.
        images = [url for url in images if url.startswith("/media/")]
    session_db.add(ProductSnapshot(
        product_id=product.id, scraped_at=now, title=title, price=price,
        status=status, description=_normalize_text(normalized.get("description")),
        image_urls="|".join(images),
    ))
    if product_has_pricing_config(product):
        update_product_selling_price(product.id, session=session_db)
