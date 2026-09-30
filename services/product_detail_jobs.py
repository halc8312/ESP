"""Durable, owner-scoped deferred details on the existing scrape queue.

The Product row is the request ledger. Each request gets a fresh fencing token;
late workers cannot write after deletion, source/shop changes or lease recovery.
NULL detail state continues to mean a legacy, fully fetched product.
"""
from __future__ import annotations

import logging
import os
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import String, cast, exists, func, or_

from database import create_isolated_session
from models import Product, ProductSnapshot, Shop, User
from services.media_queue import resolve_queue_backend_name, resolve_redis_url, resolve_scrape_queue_name
from services.scrape_observation import classify_scrape_failure, record_observation_safely
from time_utils import utc_now

logger = logging.getLogger(__name__)
_SHOP_UNSET = object()
_LEASE_SECONDS = 1800
_JOB_TIMEOUT_SECONDS = 1200
_OWNER_ACTIVE_LIMIT = 20
_GLOBAL_ACTIVE_LIMIT = 100
_local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="product-details")
_local_jobs = {}


def _scope_key(user_id, shop_id):
    # Private, never serialized or logged. The portable representation also
    # allows recovery to reject old scope before applying candidate limits.
    return f"{user_id}:{shop_id if shop_id is not None else 0}"


def _current_scope_expression():
    return cast(Product.user_id, String) + ":" + cast(func.coalesce(Product.shop_id, 0), String)


def _owned_product_query(session, product_id, user_id):
    return session.query(Product).filter(
        Product.id == product_id, Product.user_id == user_id,
        Product.deleted_at.is_(None),
        or_(Product.shop_id.is_(None), Product.shop_id.in_(
            session.query(Shop.id).filter(Shop.user_id == user_id),
        )),
    )


def _lock_queue_admission(session):
    """Serialize the short cross-owner capacity check without extra resources.

    The oldest User row is a stable database guard. A no-op UPDATE provides the
    same write serialization on SQLite, where SELECT FOR UPDATE is ignored.
    No user field changes or network calls occur inside this transaction.
    """
    row = session.query(User.id).order_by(User.id).with_for_update().first()
    if row is None:
        return
    session.query(User).filter(User.id == row[0]).update(
        {User.username: User.username}, synchronize_session=False,
    )


def _has_queue_capacity(session, owner_id, now):
    active = session.query(Product).filter(
        Product.deleted_at.is_(None), Product.detail_fetch_state.in_(("queued", "running")),
        Product.detail_lease_expires_at > now,
    )
    return active.count() < _GLOBAL_ACTIVE_LIMIT and active.filter(Product.user_id == owner_id).count() < _OWNER_ACTIVE_LIMIT


def _dispatch_detail_job(product_id, user_id, source_url, job_id, shop_id):
    args = (product_id, user_id, source_url, job_id)
    if resolve_queue_backend_name() == "rq":
        from redis import Redis
        from services.rq_compat import import_rq_queue

        Queue = import_rq_queue()
        queue = Queue(resolve_scrape_queue_name(), connection=Redis.from_url(
            resolve_redis_url(), socket_connect_timeout=5, socket_timeout=5,
        ))
        queue.enqueue_call(
            func="services.product_detail_jobs.run_product_detail_job",
            args=args, kwargs={"expected_shop_id": shop_id}, job_id=job_id,
            timeout=_JOB_TIMEOUT_SECONDS, result_ttl=0, failure_ttl=604800,
            description=f"fetch selected product {product_id} details",
        )
    else:
        future = _local_executor.submit(run_product_detail_job, *args, expected_shop_id=shop_id)
        _local_jobs[job_id] = future
        future.add_done_callback(lambda completed: _local_jobs.pop(job_id, None))


def _keep_existing_queued_request(product_id, job_id):
    """A normal Redis wait may exceed a lease; never append it again blindly."""
    if resolve_queue_backend_name() == "rq":
        try:
            from redis import Redis
            from rq.job import Job
            from rq.exceptions import NoSuchJobError

            try:
                job = Job.fetch(job_id, connection=Redis.from_url(
                    resolve_redis_url(), socket_connect_timeout=5, socket_timeout=5,
                ))
                status = job.get_status(refresh=True)
                status = getattr(status, "value", status)
                exists = status in ("queued", "deferred", "scheduled", "started")
            except NoSuchJobError:
                exists = False
        except Exception as exc:
            # Unknown Redis state must not create duplicate queue entries.
            logger.warning("Detail queue inspection unavailable error_type=%s", type(exc).__name__)
            exists = True
    else:
        future = _local_jobs.get(job_id)
        exists = future is not None and not future.done()
    if not exists:
        return False
    session = create_isolated_session()
    try:
        product = session.get(Product, product_id)
        if product is None:
            return False
        scope_key = _scope_key(product.user_id, product.shop_id)
        refreshed = session.query(Product).filter(
            Product.id == product_id, Product.detail_job_id == job_id,
            Product.detail_fetch_state == "queued", Product.deleted_at.is_(None),
            Product.detail_source_url == Product.source_url,
            Product.detail_scope_key == scope_key,
            Product.user_id == product.user_id, Product.shop_id == product.shop_id,
        ).update({Product.detail_lease_expires_at: utc_now() + timedelta(seconds=_LEASE_SECONDS)}, synchronize_session=False)
        session.commit()
    finally:
        session.close()
    return bool(refreshed)


def _fail_request(product_id, job_id, code, *, queued_only=False, user_id=None, expected_source_url=None, expected_shop_id=_SHOP_UNSET):
    session = create_isolated_session()
    try:
        query = session.query(Product).filter(
            Product.id == product_id, Product.detail_job_id == job_id,
            Product.deleted_at.is_(None),
            Product.detail_fetch_state.in_(("queued",) if queued_only else ("queued", "running")),
        )
        if user_id is not None:
            query = query.filter(Product.user_id == user_id)
        if expected_source_url is not None:
            query = query.filter(Product.source_url == expected_source_url, Product.detail_source_url == expected_source_url)
        if expected_shop_id is not _SHOP_UNSET:
            query = query.filter(Product.shop_id == expected_shop_id)
            if user_id is not None:
                query = query.filter(Product.detail_scope_key == _scope_key(user_id, expected_shop_id))
        product = query.with_for_update().first()
        if product is None:
            return False
        failures = min(10, (product.detail_fail_count or 0) + 1)
        changed = query.update({
            Product.detail_fetch_state: "failed", Product.detail_fail_count: failures,
            Product.detail_error_code: code, Product.detail_lease_expires_at: None,
            Product.detail_retry_at: utc_now() + timedelta(seconds=min(3600, 30 * 2 ** (failures - 1))),
        }, synchronize_session=False)
        session.commit()
        return bool(changed)
    finally:
        session.close()


def enqueue_product_details(product_ids, user_id, *, shop_id=_SHOP_UNSET, translate=False, respect_backoff=False):
    """Queue only selected incomplete products, deduplicating concurrent requests.

    Explicit ``shop_id=None`` selects unscoped products; omitting it accepts any
    owned shop. Failed requests respect persisted exponential retry backoff.
    Public catalog requests use ``respect_backoff=True``: a customer's request
    cannot clear a future cooldown solely from missing or changed metadata.
    Authorized owner selection may reset a corrected source or shop immediately.
    """
    summary = {"queued": 0, "skipped": 0, "failed": 0, "pending": 0, "product_ids": []}
    seen = set()
    dispatch_unavailable = False
    for raw_id in product_ids or ():
        if isinstance(raw_id, bool):
            summary["skipped"] += 1
            continue
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            summary["skipped"] += 1
            continue
        if product_id <= 0 or product_id in seen:
            continue
        seen.add(product_id)
        # Check an expired queued request outside the admission transaction:
        # Redis may still be holding it behind older detail tasks. Refreshing
        # that lease must not nest a second DB write beneath the guard lock.
        peek_session = create_isolated_session()
        try:
            peek_query = _owned_product_query(peek_session, product_id, user_id)
            if shop_id is not _SHOP_UNSET:
                peek_query = peek_query.filter(Product.shop_id == shop_id)
            peek = peek_query.first()
            expired_queued_id = peek.detail_job_id if peek and peek.detail_fetch_state == "queued" and peek.detail_source_url == peek.source_url and peek.detail_scope_key == _scope_key(peek.user_id, peek.shop_id) and (
                peek.detail_lease_expires_at is None or peek.detail_lease_expires_at <= utc_now()
            ) else None
        finally:
            peek_session.close()
        if expired_queued_id:
            _keep_existing_queued_request(product_id, expired_queued_id)
        session = create_isolated_session()
        dispatch = None
        try:
            _lock_queue_admission(session)
            query = _owned_product_query(session, product_id, user_id)
            if shop_id is not _SHOP_UNSET:
                query = query.filter(Product.shop_id == shop_id)
            product = query.with_for_update().first()
            now = utc_now()
            if product is None or product.detail_fetch_state in (None, "complete"):
                summary["skipped"] += 1
                continue
            if translate:
                product.detail_translate_requested = True
            same_request = product.detail_source_url == product.source_url and product.detail_scope_key == _scope_key(user_id, product.shop_id)
            if not same_request and (
                not respect_backoff or product.detail_retry_at is None or product.detail_retry_at <= now
            ):
                # A corrected URL/shop is a new request, not a retry of the
                # old failed source. Shared site cooldowns still apply.
                product.detail_retry_at = None
                product.detail_fail_count = 0
                product.detail_error_code = None
            if product.detail_fetch_state in ("queued", "running") and same_request and product.detail_lease_expires_at and product.detail_lease_expires_at > now:
                session.commit()
                summary["skipped"] += 1
                continue
            if product.detail_retry_at and product.detail_retry_at > now:
                session.commit()
                summary["skipped"] += 1
                continue
            if dispatch_unavailable or not _has_queue_capacity(session, user_id, now):
                # Demand is durable but absent from Redis until a fair refill.
                # Unselected cards retain NULL detail_source_url and stay out.
                product.detail_fetch_state = "pending"
                product.detail_source_url = product.source_url
                product.detail_scope_key = _scope_key(user_id, product.shop_id)
                product.detail_job_id = None
                product.detail_lease_expires_at = None
                session.commit()
                summary["skipped"] += 1
                continue
            old_token = product.detail_job_id
            old_state = product.detail_fetch_state
            new_token = f"product-detail-{uuid.uuid4().hex}"
            source_url = product.source_url
            expected_shop_id = product.shop_id
            claimed = session.query(Product).filter(
                Product.id == product_id, Product.user_id == user_id,
                Product.detail_job_id == old_token,
                Product.detail_fetch_state == old_state,
                Product.source_url == source_url, Product.shop_id == expected_shop_id,
                Product.deleted_at.is_(None),
                or_(Product.shop_id.is_(None), Product.shop_id.in_(session.query(Shop.id).filter(Shop.user_id == user_id))),
            ).update({
                Product.detail_fetch_state: "queued", Product.detail_job_id: new_token,
                Product.detail_source_url: source_url,
                Product.detail_scope_key: _scope_key(user_id, expected_shop_id),
                Product.detail_lease_expires_at: now + timedelta(seconds=_LEASE_SECONDS),
                Product.detail_retry_at: None, Product.detail_error_code: None,
                Product.detail_translate_requested: bool(product.detail_translate_requested),
            }, synchronize_session=False)
            session.commit()
            if claimed:
                dispatch = (product_id, user_id, source_url, new_token, expected_shop_id)
            else:
                summary["skipped"] += 1
        except Exception:
            session.rollback()
            logger.exception("Unable to claim product detail request product_id=%s", product_id)
            summary["failed"] += 1
        finally:
            session.close()
        if dispatch is not None:
            try:
                _dispatch_detail_job(*dispatch)
                summary["queued"] += 1
                summary["product_ids"].append(product_id)
            except Exception as exc:
                dispatch_unavailable = True
                # If Redis accepted a job before a transport failure, a running
                # worker's claim wins. Otherwise the row remains retryable.
                if not _keep_existing_queued_request(product_id, dispatch[3]):
                    _fail_request(product_id, dispatch[3], "enqueue_failed", queued_only=True,
                        user_id=user_id, expected_source_url=dispatch[2], expected_shop_id=dispatch[4])
                logger.warning("Product detail enqueue failed product_id=%s error_type=%s", product_id, type(exc).__name__)
                summary["failed"] += 1
    if seen:
        session = create_isolated_session()
        try:
            query = session.query(Product).filter(
                Product.id.in_(seen), Product.user_id == user_id,
                Product.deleted_at.is_(None),
                Product.detail_fetch_state.in_(("pending", "queued", "running")),
                or_(Product.shop_id.is_(None), Product.shop_id.in_(session.query(Shop.id).filter(Shop.user_id == user_id))),
            )
            if shop_id is not _SHOP_UNSET:
                query = query.filter(Product.shop_id == shop_id)
            summary["pending"] = query.count()
        finally:
            session.close()
    return summary


def _scrape_detail(site, url):
    from importlib import import_module
    from services.scrape_safety import validate_marketplace_url

    validate_marketplace_url(url, site, kind="detail")
    modules = {
        "mercari": "mercari_db", "yahoo": "yahoo_db", "rakuma": "rakuma_db",
        "surugaya": "surugaya_db", "offmall": "offmall_db", "yahuoku": "yahuoku_db",
        "snkrdunk": "snkrdunk_db", "recordcity": "recordcity_db",
    }
    if site not in modules:
        raise ValueError("detail_unsupported_site")
    item = import_module(modules[site]).scrape_single_item(url, headless=True)
    if isinstance(item, (list, tuple)):
        item = item[0] if len(item) == 1 else None
    if not isinstance(item, dict):
        raise ValueError("detail_invalid_result")
    return item


def run_product_detail_job(product_id, user_id, expected_source_url, jobid, *, expected_shop_id=_SHOP_UNSET):
    """Worker entrypoint. Claim, fetch, recheck and atomically complete one row."""
    session = create_isolated_session()
    site = None
    captured_shop = None
    try:
        query = _owned_product_query(session, product_id, user_id).filter(
            Product.detail_job_id == jobid, Product.detail_fetch_state == "queued",
            Product.source_url == expected_source_url,
            Product.detail_source_url == expected_source_url,
        )
        if expected_shop_id is not _SHOP_UNSET:
            query = query.filter(Product.shop_id == expected_shop_id)
        product = query.with_for_update().first()
        if product is None:
            return {"status": "stale", "product_id": product_id}
        site, captured_shop = product.site, product.shop_id
        claimed = query.filter(Product.site == site, Product.shop_id == captured_shop,
            Product.detail_scope_key == _scope_key(user_id, captured_shop)).update({
            Product.detail_fetch_state: "running",
            Product.detail_lease_expires_at: utc_now() + timedelta(seconds=_LEASE_SECONDS),
        }, synchronize_session=False)
        session.commit()
        if not claimed:
            return {"status": "stale", "product_id": product_id}
    finally:
        session.close()

    try:
        from services.marketplace_access import request_budget

        with request_budget(max_requests=120, max_seconds=900):
            item = _scrape_detail(site, expected_source_url)
            # Validate before image IO and keep that IO outside Product/Shop
            # locks. A unique request namespace prevents a stale downloader
            # from overwriting the files selected by a later successful job.
            from services.product_service import cache_deferred_detail_images
            from services.scrape_result_policy import evaluate_persistence
            from services.search_result_quality import search_item_identity

            image_session = create_isolated_session()
            try:
                current = _owned_product_query(image_session, product_id, user_id).filter(
                    Product.detail_job_id == jobid, Product.detail_fetch_state == "running",
                    Product.source_url == expected_source_url, Product.site == site,
                    Product.detail_source_url == expected_source_url,
                    Product.detail_scope_key == _scope_key(user_id, captured_shop),
                    Product.shop_id == captured_shop, Product.detail_lease_expires_at > utc_now(),
                ).first()
                if current is None:
                    return {"status": "stale", "product_id": product_id}
                identity = search_item_identity(item, site=site)
                if identity is None or identity != search_item_identity({"url": expected_source_url}, site=site):
                    raise ValueError("detail_identity_mismatch")
                image_action = evaluate_persistence(site, item, item.get("_scrape_meta"), current)
                if image_action == "reject":
                    raise ValueError("detail_unverified")
            finally:
                image_session.close()
            cached_images = cache_deferred_detail_images(item, product_id, jobid) if image_action == "allow_full" else []
            session = create_isolated_session()
            try:
                if captured_shop is not None:
                    # Keep ownership valid through commit, not just at the prior
                    # Product read. A shop transfer must wait for this transaction.
                    shop = session.query(Shop.id).filter(Shop.id == captured_shop, Shop.user_id == user_id).with_for_update().first()
                    if shop is None:
                        return {"status": "stale", "product_id": product_id}
                    locked = session.query(Shop).filter(Shop.id == captured_shop, Shop.user_id == user_id).update({Shop.name: Shop.name}, synchronize_session=False)
                    if not locked:
                        return {"status": "stale", "product_id": product_id}
                final_query = _owned_product_query(session, product_id, user_id).filter(
                    Product.detail_job_id == jobid, Product.detail_fetch_state == "running",
                    Product.source_url == expected_source_url, Product.site == site,
                    Product.detail_source_url == expected_source_url,
                    Product.detail_scope_key == _scope_key(user_id, captured_shop),
                    Product.shop_id == captured_shop,
                    Product.detail_lease_expires_at > utc_now(),
                )
                product = final_query.with_for_update().first()
                if product is None:
                    return {"status": "stale", "product_id": product_id}
                # This UPDATE is a write fence on SQLite too, where FOR UPDATE is
                # ignored. Concurrent source edits either commit first or wait.
                fenced = final_query.filter(Product.detail_lease_expires_at > utc_now()).update(
                    {Product.detail_fetch_state: "complete"}, synchronize_session=False,
                )
                if not fenced:
                    session.rollback()
                    return {"status": "stale", "product_id": product_id}
                from services.product_service import save_scraped_product_detail

                save_scraped_product_detail(session, product, item, cached_images=cached_images)
                product.detail_fetch_state = "complete"
                product.detail_lease_expires_at = None
                product.detail_retry_at = None
                product.detail_fail_count = 0
                product.detail_error_code = None
                translation_id = _create_completion_translation(session, product) if product.detail_translate_requested else None
                product.detail_translate_requested = False
                from services.scrape_job_runtime import assert_current_job_active

                assert_current_job_active()
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()
        if translation_id:
            _dispatch_translation(translation_id)
        record_observation_safely(site=site, route="detail", outcome="success", success_count=1)
        return {"status": "complete", "product_id": product_id}
    except BaseException as exc:
        reason = classify_scrape_failure(exc, default="invalid_result")
        failed = _fail_request(product_id, jobid, reason, user_id=user_id,
            expected_source_url=expected_source_url, expected_shop_id=captured_shop)
        if failed:
            record_observation_safely(site=site, route="detail", outcome="failure", reason=reason, error_count=1)
        logger.warning("Product detail failed product_id=%s error_type=%s", product_id, type(exc).__name__)
        if not isinstance(exc, Exception):
            raise
        return {"status": "failed" if failed else "stale", "product_id": product_id}


def _create_completion_translation(session, product):
    from services.translator.source_hash import compute_source_hash
    from services.translator.suggestion_store import create_suggestion

    title = (product.custom_title or product.last_title or "").strip()
    latest = session.query(ProductSnapshot).filter_by(product_id=product.id).order_by(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc()).first()
    description = (product.custom_description or (latest.description if latest else "") or "").strip()
    if not title and not description:
        return None
    job_id = str(uuid.uuid4())
    create_suggestion(
        session=session, job_id=job_id, product_id=product.id, user_id=product.user_id,
        scope="full", provider=os.environ.get("TRANSLATOR_BACKEND", "argos").lower(),
        source_title=title or None, source_description=description or None,
        source_title_hash=compute_source_hash(title) or None,
        source_description_hash=compute_source_hash(description) or None, auto_apply=True,
    )
    return job_id


def _dispatch_translation(job_id):
    try:
        if resolve_queue_backend_name() == "rq":
            from services.media_queue import enqueue_media_job

            enqueue_media_job(job_id=job_id, func="jobs.translation_tasks.execute_translation_job", args=(job_id,))
        else:
            from jobs.translation_tasks import execute_translation_job

            _local_executor.submit(execute_translation_job, job_id)
    except Exception as exc:
        # The durable queued suggestion is handled by existing translation
        # startup recovery. Detail completion remains committed.
        logger.warning("Deferred translation enqueue failed error_type=%s", type(exc).__name__)


def recover_product_detail_jobs(*, limit=100):
    """Reissue only lost queued/expired running selections, never every card."""
    session = create_isolated_session()
    try:
        now = utc_now()
        maximum = max(1, min(500, int(limit)))
        # Sample demand by owner first, so one owner's hundreds of cards do
        # not hide another owner's first selected item behind a global LIMIT.
        recoverable = or_(
            (Product.detail_fetch_state == "pending") & Product.detail_source_url.is_not(None),
            Product.detail_fetch_state.in_(("queued", "running")) & or_(
                Product.detail_lease_expires_at.is_(None), Product.detail_lease_expires_at <= now,
            ),
        )
        # An old owner/shop's selection is not consent in a new scope. Only
        # explicit enqueue may re-arm that request. Filter invalid shops and
        # stale scopes before LIMIT, so they cannot starve later valid rows.
        eligible = session.query(Product).filter(
            Product.deleted_at.is_(None), recoverable,
            Product.detail_scope_key == _current_scope_expression(),
            or_(Product.shop_id.is_(None), exists().where(
                Shop.id == Product.shop_id, Shop.user_id == Product.user_id,
            )),
        )
        owner_ids = [row[0] for row in eligible.with_entities(Product.user_id).distinct().order_by(Product.user_id).limit(maximum).all()]
        counts = dict(session.query(Product.user_id, func.count(Product.id)).filter(
            Product.deleted_at.is_(None), Product.detail_fetch_state.in_(("queued", "running")),
            Product.detail_lease_expires_at > now,
        ).group_by(Product.user_id).all())
        owner_ids.sort(key=lambda owner: (counts.get(owner, 0), owner))
        by_owner = {}
        for owner in owner_ids:
            by_owner[owner] = deque(row[0] for row in eligible.with_entities(Product.id).filter(
                Product.user_id == owner,
            ).order_by(Product.id).limit(min(_OWNER_ACTIVE_LIMIT, maximum)).all())
        rows = []
        while len(rows) < maximum and any(by_owner.values()):
            for owner in owner_ids:
                if by_owner[owner]:
                    rows.append((by_owner[owner].popleft(), owner))
                    if len(rows) == maximum:
                        break
    finally:
        session.close()
    summary = {"queued": 0, "skipped": 0, "failed": 0}
    for product_id, owner_id in rows:
        session = create_isolated_session()
        try:
            product = session.get(Product, product_id)
            job_id = product.detail_job_id if product and product.detail_fetch_state == "queued" and product.detail_source_url == product.source_url and product.detail_scope_key == _scope_key(product.user_id, product.shop_id) else None
        finally:
            session.close()
        if job_id and _keep_existing_queued_request(product_id, job_id):
            summary["skipped"] += 1
            continue
        result = enqueue_product_details([product_id], owner_id)
        for key in summary:
            summary[key] += result[key]
        if result["failed"]:
            # A broken Redis connection is shared; retrying every selected
            # product in this pass would block both the worker and the web.
            break
    return summary
