"""Bounded, durable image-only delivery for shallow listing products.

Every demand captures its exact source snapshot and owner/shop. One RQ batch
contains at most ten thumbnails; at most one batch per owner and five overall
can be live. Thumbnail completion never fetches or changes product details.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import exists, func, or_, select

from database import create_isolated_session
from models import Product, ProductSnapshot, ProductThumbnailJob, Shop, User
from services.media_queue import resolve_queue_backend_name, resolve_redis_url, resolve_scrape_queue_name
from time_utils import utc_now

logger = logging.getLogger(__name__)
BATCH_SIZE = 10
OWNER_BATCH_LIMIT = 1
GLOBAL_BATCH_LIMIT = 5
MAX_ATTEMPTS = 5
QUEUED_LEASE_SECONDS = 1800
RUNNING_LEASE_SECONDS = 600
BATCH_TIMEOUT_SECONDS = 600
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="product-thumbnails")
_local_batches = {}


def _same_shop(left, right):
    return or_(left == right, left.is_(None) & right.is_(None))


def _latest_snapshot_id():
    return select(ProductSnapshot.id).where(
        ProductSnapshot.product_id == ProductThumbnailJob.product_id,
    ).order_by(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc()).limit(1).correlate(ProductThumbnailJob).scalar_subquery()


def _scope_conditions():
    """Correlated predicates also work in atomic UPDATE, before any LIMIT."""
    return (
        exists().where(
            Product.id == ProductThumbnailJob.product_id,
            Product.user_id == ProductThumbnailJob.user_id,
            _same_shop(Product.shop_id, ProductThumbnailJob.shop_id),
            Product.source_url == ProductThumbnailJob.product_source_url,
            Product.site == "recordcity", Product.deleted_at.is_(None),
            Product.archived.is_not(True),
        ),
        or_(ProductThumbnailJob.shop_id.is_(None), exists().where(
            Shop.id == ProductThumbnailJob.shop_id, Shop.user_id == ProductThumbnailJob.user_id,
        )),
        exists().where(User.id == ProductThumbnailJob.user_id, User.suspended_at.is_(None)),
        ProductThumbnailJob.source_snapshot_id == _latest_snapshot_id(),
    )


def _pending_scope_conditions():
    return (*_scope_conditions(), exists().where(
        ProductSnapshot.id == ProductThumbnailJob.source_snapshot_id,
        ProductSnapshot.product_id == ProductThumbnailJob.product_id,
        ProductSnapshot.image_urls == ProductThumbnailJob.source_image_url,
    ))


def create_thumbnail_demand(session, product, snapshot):
    """Create demand inside the listing save transaction; performs no IO."""
    from services.image_service import validate_image_url
    from services.scrape_safety import validate_marketplace_url

    image = str(snapshot.image_urls or "")
    if product.site != "recordcity" or product.deleted_at is not None or product.archived is True or not image:
        return False
    try:
        validate_marketplace_url(product.source_url, "recordcity", kind="detail")
        validate_image_url(image)
        if urlsplit(image).hostname != "files.recordcity.jp":
            return False
    except Exception:
        return False
    if snapshot.id is None:
        session.flush()
    current = session.get(ProductThumbnailJob, product.id)
    if current is not None and (
        current.user_id == product.user_id and current.shop_id == product.shop_id
        and current.product_source_url == product.source_url
        and current.source_snapshot_id == snapshot.id and current.source_image_url == image
    ):
        return False
    now = utc_now()
    if current is None:
        current = ProductThumbnailJob(product_id=product.id, created_at=now)
        session.add(current)
    current.user_id = product.user_id
    current.shop_id = product.shop_id
    current.product_source_url = product.source_url
    current.source_snapshot_id = snapshot.id
    current.source_image_url = image
    current.state = "pending"
    current.job_id = None
    current.claim_token = None
    current.lease_expires_at = None
    current.retry_at = None
    current.attempts = 0
    current.dispatch_attempts = 0
    current.error_code = None
    current.managed_image_url = None
    current.updated_at = now
    return True


def _recoverable(now):
    return or_(
        ProductThumbnailJob.state == "pending",
        (ProductThumbnailJob.state == "failed") & (ProductThumbnailJob.attempts < MAX_ATTEMPTS)
        & or_(ProductThumbnailJob.retry_at.is_(None), ProductThumbnailJob.retry_at <= now),
        ProductThumbnailJob.state.in_(("queued", "running")) & or_(
            ProductThumbnailJob.lease_expires_at.is_(None), ProductThumbnailJob.lease_expires_at <= now,
        ),
    )


def _existing_batch_is_alive(job_id):
    if resolve_queue_backend_name() != "rq":
        future = _local_batches.get(job_id)
        return future is not None and not future.done()
    try:
        from redis import Redis
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        try:
            job = Job.fetch(job_id, connection=Redis.from_url(resolve_redis_url(), socket_connect_timeout=3, socket_timeout=3))
            status = job.get_status(refresh=True)
            # Only an explicit terminal state proves the old work horse is
            # gone. Missing/unrecognized RQ status must retain its capacity.
            return getattr(status, "value", status) not in ("finished", "failed", "stopped", "canceled")
        except NoSuchJobError:
            return False
    except Exception as exc:
        logger.warning("Thumbnail queue inspection unavailable error_type=%s", type(exc).__name__)
        # Unknown queue state never creates a duplicate batch.
        return True


def _renew_queued_batch(job_id):
    session = create_isolated_session()
    try:
        session.query(ProductThumbnailJob).filter(
            ProductThumbnailJob.job_id == job_id, ProductThumbnailJob.state == "queued",
            *_pending_scope_conditions(),
        ).update({ProductThumbnailJob.lease_expires_at: utc_now() + timedelta(seconds=QUEUED_LEASE_SECONDS)}, synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _dispatch_batch(user_id, job_id):
    if resolve_queue_backend_name() == "rq":
        from redis import Redis
        from services.rq_compat import import_rq_queue

        Queue = import_rq_queue()
        queue = Queue(resolve_scrape_queue_name(), connection=Redis.from_url(resolve_redis_url(), socket_connect_timeout=3, socket_timeout=3))
        queue.enqueue_call(func="services.product_thumbnail_jobs.run_thumbnail_batch",
            args=(user_id, job_id), job_id=job_id, timeout=BATCH_TIMEOUT_SECONDS,
            result_ttl=0, failure_ttl=604800, description=f"cache owner {user_id} listing thumbnails")
    else:
        future = _executor.submit(run_thumbnail_batch, user_id, job_id)
        _local_batches[job_id] = future
        future.add_done_callback(lambda completed: _local_batches.pop(job_id, None))


def _mark_batch_enqueue_failed(job_id):
    session = create_isolated_session()
    try:
        now = utc_now()
        rows = session.query(ProductThumbnailJob).filter(
            ProductThumbnailJob.job_id == job_id, ProductThumbnailJob.state == "queued",
            *_pending_scope_conditions(),
        ).with_for_update().all()
        for row in rows:
            attempts = min(MAX_ATTEMPTS, row.dispatch_attempts + 1)
            session.query(ProductThumbnailJob).filter(
                ProductThumbnailJob.product_id == row.product_id,
                ProductThumbnailJob.job_id == job_id, ProductThumbnailJob.state == "queued",
                *_pending_scope_conditions(),
            ).update({ProductThumbnailJob.state: "failed", ProductThumbnailJob.dispatch_attempts: attempts,
                ProductThumbnailJob.error_code: "enqueue_failed", ProductThumbnailJob.lease_expires_at: None,
                ProductThumbnailJob.retry_at: now + timedelta(seconds=min(1800, 30 * 2 ** (attempts - 1))),
                ProductThumbnailJob.updated_at: now}, synchronize_session=False)
        session.commit()
    finally:
        session.close()


def recover_thumbnail_jobs(*, limit_batches=GLOBAL_BATCH_LIMIT, user_id=None):
    """Refill valid demand fairly, with bounded batch admission and retry count."""
    from services.product_detail_jobs import _lock_queue_admission

    session = create_isolated_session()
    try:
        now = utc_now()
        expired = session.query(ProductThumbnailJob.job_id).filter(
            ProductThumbnailJob.state.in_(("queued", "running")),
            ProductThumbnailJob.job_id.is_not(None),
            or_(ProductThumbnailJob.lease_expires_at.is_(None), ProductThumbnailJob.lease_expires_at <= now),
            *_pending_scope_conditions(),
        ).group_by(ProductThumbnailJob.job_id).order_by(func.min(ProductThumbnailJob.lease_expires_at), ProductThumbnailJob.job_id).limit(GLOBAL_BATCH_LIMIT).all()
    finally:
        session.close()
    gone_batches = []
    for (job_id,) in expired:
        if _existing_batch_is_alive(job_id):
            _renew_queued_batch(job_id)
        else:
            gone_batches.append(job_id)

    session = create_isolated_session()
    try:
        now = utc_now()
        # A worker lost during its last permitted request cannot remain
        # displayed as running forever or acquire a sixth fetch attempt.
        exhausted = session.query(ProductThumbnailJob).filter(
            ProductThumbnailJob.state.in_(("queued", "running")),
            or_(ProductThumbnailJob.lease_expires_at.is_(None), ProductThumbnailJob.lease_expires_at <= now),
            or_(ProductThumbnailJob.attempts >= MAX_ATTEMPTS, ProductThumbnailJob.dispatch_attempts >= MAX_ATTEMPTS),
            *_pending_scope_conditions(),
        )
        exhausted = exhausted.filter(or_(ProductThumbnailJob.job_id.is_(None), ProductThumbnailJob.job_id.in_(gone_batches)))
        exhausted.update({ProductThumbnailJob.state: "failed", ProductThumbnailJob.error_code: "attempts_exhausted",
            ProductThumbnailJob.lease_expires_at: None, ProductThumbnailJob.retry_at: None,
            ProductThumbnailJob.updated_at: now}, synchronize_session=False)
        session.commit()
    finally:
        session.close()

    summary = {"queued_batches": 0, "queued_images": 0, "failed": 0}
    for _ in range(max(1, min(GLOBAL_BATCH_LIMIT, int(limit_batches)))):
        session = create_isolated_session()
        dispatch = None
        try:
            _lock_queue_admission(session)
            now = utc_now()
            active = session.query(ProductThumbnailJob).filter(
                ProductThumbnailJob.state.in_(("queued", "running")),
                or_(ProductThumbnailJob.lease_expires_at > now,
                    ProductThumbnailJob.job_id.is_not(None) & ~ProductThumbnailJob.job_id.in_(gone_batches)),
                *_scope_conditions(),
            )
            if active.with_entities(ProductThumbnailJob.job_id).distinct().count() >= GLOBAL_BATCH_LIMIT:
                break
            occupied = [row[0] for row in active.with_entities(ProductThumbnailJob.user_id).distinct().all()]
            eligible = session.query(ProductThumbnailJob).filter(
                _recoverable(now), ProductThumbnailJob.attempts < MAX_ATTEMPTS,
                ProductThumbnailJob.dispatch_attempts < MAX_ATTEMPTS,
                *_pending_scope_conditions(),
            )
            # RQ's timeout and a DB lease expiring at the same instant do not
            # prove the work horse stopped. Uninspected batches also retain
            # capacity if their lease expires during the bounded queue probes.
            # An expired upload token is never revived by this reservation.
            eligible = eligible.filter(or_(
                ~ProductThumbnailJob.state.in_(("queued", "running")),
                ProductThumbnailJob.job_id.is_(None), ProductThumbnailJob.job_id.in_(gone_batches),
            ))
            if occupied:
                eligible = eligible.filter(~ProductThumbnailJob.user_id.in_(occupied))
            if user_id is not None:
                eligible = eligible.filter(ProductThumbnailJob.user_id == user_id)
            # Most recently served owners go last. New waiting owners enter
            # before a previous owner's second batch even with thousands of rows.
            service_times = dict(session.query(ProductThumbnailJob.user_id, func.max(ProductThumbnailJob.updated_at)).group_by(ProductThumbnailJob.user_id).all())
            owners = [row[0] for row in eligible.with_entities(ProductThumbnailJob.user_id).distinct().all()]
            owners.sort(key=lambda owner: (service_times.get(owner) or now, owner))
            if not owners:
                break
            owner = owners[0]
            rows = eligible.filter(ProductThumbnailJob.user_id == owner).order_by(ProductThumbnailJob.created_at, ProductThumbnailJob.product_id).limit(BATCH_SIZE).with_for_update().all()
            if not rows:
                break
            product_ids = [row.product_id for row in rows]
            job_id = f"thumbnail-batch-{uuid.uuid4().hex}"
            changed = session.query(ProductThumbnailJob).filter(
                ProductThumbnailJob.product_id.in_(product_ids), _recoverable(now),
                ProductThumbnailJob.user_id == owner, ProductThumbnailJob.attempts < MAX_ATTEMPTS,
                ProductThumbnailJob.dispatch_attempts < MAX_ATTEMPTS,
                *_pending_scope_conditions(),
            ).update({ProductThumbnailJob.state: "queued", ProductThumbnailJob.job_id: job_id,
                ProductThumbnailJob.claim_token: None, ProductThumbnailJob.retry_at: None,
                ProductThumbnailJob.lease_expires_at: now + timedelta(seconds=QUEUED_LEASE_SECONDS),
                ProductThumbnailJob.updated_at: now}, synchronize_session=False)
            session.commit()
            if changed:
                dispatch = (owner, job_id, changed)
        finally:
            session.close()
        if dispatch:
            try:
                _dispatch_batch(dispatch[0], dispatch[1])
                summary["queued_batches"] += 1
                summary["queued_images"] += dispatch[2]
            except Exception as exc:
                if _existing_batch_is_alive(dispatch[1]):
                    _renew_queued_batch(dispatch[1])
                else:
                    _mark_batch_enqueue_failed(dispatch[1])
                logger.warning("Thumbnail enqueue failed error_type=%s", type(exc).__name__)
                summary["failed"] += 1
                break
    return summary


def enqueue_thumbnail_jobs(product_ids, user_id):
    """Listing save already created demand; admission never creates details."""
    return recover_thumbnail_jobs(user_id=user_id, limit_batches=1)


@dataclass
class ThumbnailDeliveryContext:
    job: ProductThumbnailJob
    product: Product
    snapshot: ProductSnapshot


def authorize_thumbnail_delivery(session, product_id, token, now=None):
    """Authorize and lock the exact image target, including safe complete replay."""
    now = now or utc_now()
    job = session.query(ProductThumbnailJob).filter(
        ProductThumbnailJob.product_id == product_id, ProductThumbnailJob.claim_token == token,
        or_(ProductThumbnailJob.state == "complete", (ProductThumbnailJob.state == "running") & (ProductThumbnailJob.lease_expires_at > now)),
        *_scope_conditions(),
    ).first()
    if job is None:
        return None
    if job.shop_id is not None:
        owned = session.query(Shop).filter(Shop.id == job.shop_id, Shop.user_id == job.user_id).with_for_update().first()
        if owned is None:
            return None
        if session.query(Shop).filter(Shop.id == job.shop_id, Shop.user_id == job.user_id).update({Shop.name: Shop.name}, synchronize_session=False) != 1:
            return None
    product = session.query(Product).filter(Product.id == product_id, Product.user_id == job.user_id,
        Product.shop_id == job.shop_id, Product.source_url == job.product_source_url,
        Product.deleted_at.is_(None), Product.site == "recordcity").with_for_update().first()
    if product is None:
        return None
    if session.query(Product).filter(Product.id == product_id, Product.user_id == job.user_id,
        Product.shop_id == job.shop_id, Product.source_url == job.product_source_url,
        Product.deleted_at.is_(None)).update({Product.id: Product.id}, synchronize_session=False) != 1:
        return None
    snapshot = session.query(ProductSnapshot).filter(
        ProductSnapshot.id == job.source_snapshot_id, ProductSnapshot.product_id == product_id,
    ).with_for_update().first()
    if snapshot is None:
        return None
    session.expire(job)
    job = session.query(ProductThumbnailJob).filter(
        ProductThumbnailJob.product_id == product_id, ProductThumbnailJob.claim_token == token,
        *_scope_conditions(),
    ).with_for_update().first()
    if job is None:
        return None
    if job.source_snapshot_id != snapshot.id or job.product_source_url != product.source_url:
        return None
    if job.state == "complete":
        if not job.managed_image_url or snapshot.image_urls != job.managed_image_url:
            return None
    elif job.state != "running" or job.lease_expires_at is None or job.lease_expires_at <= now or snapshot.image_urls != job.source_image_url:
        return None
    return ThumbnailDeliveryContext(job, product, snapshot)


def finalize_thumbnail_delivery(session, context, managed_url):
    """Caller commits this image-only CAS together with managed file delivery."""
    job = context.job
    allowed = {f"/media/product-delivery/thumbnail/{job.product_id}/{job.claim_token}/0.{extension}" for extension in ("png", "jpg", "webp", "gif")}
    if managed_url not in allowed:
        return False
    if job.state == "complete":
        return bool(session.query(ProductThumbnailJob.product_id).filter(
            ProductThumbnailJob.product_id == job.product_id,
            ProductThumbnailJob.claim_token == job.claim_token,
            ProductThumbnailJob.state == "complete",
            ProductThumbnailJob.user_id == context.product.user_id,
            ProductThumbnailJob.shop_id == context.product.shop_id,
            ProductThumbnailJob.product_source_url == context.product.source_url,
            ProductThumbnailJob.source_snapshot_id == context.snapshot.id,
            ProductThumbnailJob.managed_image_url == managed_url,
            *_scope_conditions(),
            exists().where(ProductSnapshot.id == context.snapshot.id, ProductSnapshot.image_urls == managed_url),
        ).first())
    now = utc_now()
    claimed = session.query(ProductThumbnailJob).filter(
        ProductThumbnailJob.product_id == job.product_id, ProductThumbnailJob.claim_token == job.claim_token,
        ProductThumbnailJob.state == "running", ProductThumbnailJob.lease_expires_at > now,
        ProductThumbnailJob.user_id == context.product.user_id,
        ProductThumbnailJob.shop_id == context.product.shop_id,
        ProductThumbnailJob.product_source_url == context.product.source_url,
        ProductThumbnailJob.source_snapshot_id == context.snapshot.id,
        ProductThumbnailJob.source_image_url == job.source_image_url,
        *_pending_scope_conditions(),
    ).update({ProductThumbnailJob.state: "complete", ProductThumbnailJob.managed_image_url: managed_url,
        ProductThumbnailJob.lease_expires_at: None, ProductThumbnailJob.retry_at: None,
        ProductThumbnailJob.error_code: None, ProductThumbnailJob.updated_at: now}, synchronize_session=False)
    if claimed != 1:
        return False
    changed = session.query(ProductSnapshot).filter(
        ProductSnapshot.id == job.source_snapshot_id, ProductSnapshot.product_id == job.product_id,
        ProductSnapshot.image_urls == job.source_image_url,
    ).update({ProductSnapshot.image_urls: managed_url}, synchronize_session=False)
    if changed != 1:
        # The endpoint removes a newly rejected slot while its locks are still
        # held, then rolls back. Transaction ownership stays with the caller.
        return False
    return True


def _fail_thumbnail(product_id, token, code, *, retry_seconds=None):
    session = create_isolated_session()
    try:
        row = session.query(ProductThumbnailJob).filter(
            ProductThumbnailJob.product_id == product_id, ProductThumbnailJob.claim_token == token,
            ProductThumbnailJob.state == "running", *_pending_scope_conditions(),
        ).first()
        if row is None:
            return False
        delay = retry_seconds if retry_seconds is not None else min(1800, 30 * 2 ** max(0, row.attempts - 1))
        changed = session.query(ProductThumbnailJob).filter(
            ProductThumbnailJob.product_id == product_id, ProductThumbnailJob.claim_token == token,
            ProductThumbnailJob.state == "running", *_pending_scope_conditions(),
        ).update({ProductThumbnailJob.state: "failed", ProductThumbnailJob.error_code: code,
            ProductThumbnailJob.lease_expires_at: None,
            ProductThumbnailJob.retry_at: utc_now() + timedelta(seconds=delay),
            ProductThumbnailJob.updated_at: utc_now()}, synchronize_session=False)
        session.commit()
        return bool(changed)
    finally:
        session.close()


def _run_thumbnail(product_id, user_id, batch_id):
    session = create_isolated_session()
    token = uuid.uuid4().hex
    try:
        now = utc_now()
        query = session.query(ProductThumbnailJob).filter(
            ProductThumbnailJob.product_id == product_id, ProductThumbnailJob.user_id == user_id,
            ProductThumbnailJob.job_id == batch_id, ProductThumbnailJob.state == "queued",
            ProductThumbnailJob.attempts < MAX_ATTEMPTS, *_pending_scope_conditions(),
        )
        row = query.with_for_update().first()
        if row is None:
            return "stale"
        source_image = row.source_image_url
        source_url = row.product_source_url
        changed = query.update({ProductThumbnailJob.state: "running", ProductThumbnailJob.claim_token: token,
            ProductThumbnailJob.lease_expires_at: now + timedelta(seconds=RUNNING_LEASE_SECONDS),
            ProductThumbnailJob.updated_at: now}, synchronize_session=False)
        session.commit()
        if changed != 1:
            return "stale"
    finally:
        session.close()
    fetch_attempted = False
    try:
        from services.image_service import _response_status, download_external_image
        from services.marketplace_access import marketplace_access, observe_access_response
        from services.product_image_delivery import deliver_image_bytes
        from services.scrape_job_runtime import assert_current_job_active

        assert_current_job_active()
        with marketplace_access(source_url, timeout_seconds=30, consume_request=False) as parent:
            @contextmanager
            def admit_hop(current_url):
                nonlocal fetch_attempted
                with marketplace_access(source_url, timeout_seconds=30, parent_lease=parent):
                    # Each physical redirect hop consumes the shared budget.
                    # Only the first admitted hop spends one thumbnail attempt.
                    if not fetch_attempted:
                        assert_current_job_active()
                        session = create_isolated_session()
                        try:
                            counted = session.query(ProductThumbnailJob).filter(
                                ProductThumbnailJob.product_id == product_id,
                                ProductThumbnailJob.claim_token == token,
                                ProductThumbnailJob.state == "running",
                                ProductThumbnailJob.lease_expires_at > utc_now(),
                                ProductThumbnailJob.attempts < MAX_ATTEMPTS,
                                *_pending_scope_conditions(),
                            ).update({ProductThumbnailJob.attempts: ProductThumbnailJob.attempts + 1,
                                ProductThumbnailJob.dispatch_attempts: 0}, synchronize_session=False)
                            session.commit()
                        finally:
                            session.close()
                        if counted != 1:
                            raise ValueError("stale_thumbnail_claim")
                        fetch_attempted = True
                    yield

            def observe_hop(current_url, response):
                observe_access_response(source_url, _response_status(response),
                    headers={"Retry-After": str(response.headers.get("Retry-After", ""))})

            content, extension = download_external_image(source_image,
                request_admission=admit_hop, response_observer=observe_hop)
        assert_current_job_active()
        managed_url = deliver_image_bytes(product_id, token, 0, content, kind="thumbnail")
        session = create_isolated_session()
        try:
            context = authorize_thumbnail_delivery(session, product_id, token)
            if context is None or not finalize_thumbnail_delivery(session, context, managed_url):
                session.rollback()
                return "stale"
            assert_current_job_active()
            session.commit()
        finally:
            session.close()
        return "complete"
    except BaseException as exc:
        # A lost response cannot turn the web's committed image into a failure.
        retry_seconds = None
        code = "image_delivery_failed" if fetch_attempted else "access_wait"
        if not fetch_attempted or getattr(exc, "retry_after_seconds", None) is not None:
            retry_seconds = min(3600, max(60, float(getattr(exc, "retry_after_seconds", None) or 60)))
        if not _fail_thumbnail(product_id, token, code, retry_seconds=retry_seconds):
            session = create_isolated_session()
            try:
                row = session.get(ProductThumbnailJob, product_id)
                if row and row.claim_token == token and row.state == "complete":
                    return "complete"
            finally:
                session.close()
            return "stale"
        logger.warning("Thumbnail failed product_id=%s error_type=%s", product_id, type(exc).__name__)
        if not isinstance(exc, Exception):
            raise
        return "failed"


def run_thumbnail_batch(user_id, batch_id):
    """Drain a bounded owner batch and refill the next fair batch immediately."""
    from services.marketplace_access import request_budget

    summary = {"complete": 0, "failed": 0, "stale": 0}
    session = create_isolated_session()
    try:
        ids = [row[0] for row in session.query(ProductThumbnailJob.product_id).filter(
            ProductThumbnailJob.user_id == user_id, ProductThumbnailJob.job_id == batch_id,
            ProductThumbnailJob.state == "queued", *_pending_scope_conditions(),
        ).order_by(ProductThumbnailJob.product_id).limit(BATCH_SIZE).all()]
    finally:
        session.close()
    try:
        with request_budget(max_requests=30, max_seconds=300):
            for product_id in ids:
                from services.scrape_job_runtime import assert_current_job_active

                assert_current_job_active()
                result = _run_thumbnail(product_id, user_id, batch_id)
                summary[result] += 1
    finally:
        session = create_isolated_session()
        try:
            # A deadline/termination leaves unattempted rows available to the
            # next batch immediately, without spending a fetch attempt.
            session.query(ProductThumbnailJob).filter(
                ProductThumbnailJob.user_id == user_id, ProductThumbnailJob.job_id == batch_id,
                ProductThumbnailJob.state == "queued", *_pending_scope_conditions(),
            ).update({ProductThumbnailJob.state: "pending", ProductThumbnailJob.job_id: None,
                ProductThumbnailJob.claim_token: None, ProductThumbnailJob.lease_expires_at: None,
                ProductThumbnailJob.updated_at: utc_now()}, synchronize_session=False)
            session.commit()
        finally:
            session.close()
        try:
            recover_thumbnail_jobs()
        except Exception as exc:
            logger.warning("Thumbnail refill unavailable error_type=%s", type(exc).__name__)
    return summary
