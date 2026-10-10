"""
Monitor Service - Lightweight Patrol for price/stock updates.
Uses efficient patrol scrapers that only fetch price and stock data.
Non-Mercari sites use HTTP-only fetching (Scrapling) to avoid launching Chrome.
"""
import logging
import time
from datetime import timedelta
from sqlalchemy import asc, func, or_, text
from database import create_isolated_session
from models import Product, Variant, PriceList, PriceListItem, Shop
from services.pricing_service import product_has_pricing_config, update_product_selling_price
from services.scrape_result_policy import normalize_price_for_persistence, normalize_status_for_persistence
from services.scrape_observation import classify_scrape_failure, record_observation_safely
from time_utils import utc_now
from utils import is_valid_detail_url
from services.operation_deadline import OperationDeadlineExceeded, deadline_scope, check_deadline
from services.patrol_supervisor import (
    PatrolCancelled, PatrolSupervisorQuarantined, patrol_item_timeout, patrol_batch_timeout,
)
from services.scrape_alerts import report_patrol_result

# Import lightweight patrol scrapers
from services.patrol.mercari_patrol import MercariPatrol
from services.patrol.yahoo_patrol import YahooPatrol
from services.patrol.rakuma_patrol import RakumaPatrol
from services.patrol.surugaya_patrol import SurugayaPatrol
from services.patrol.offmall_patrol import OffmallPatrol
from services.patrol.yahuoku_patrol import YahuokuPatrol
from services.patrol.snkrdunk_patrol import SnkrdunkPatrol
from services.patrol.recordcity_patrol import RecordCityPatrol

logger = logging.getLogger("patrol")

# Sites that require a browser (Selenium/Chrome)
_BROWSER_SITES = frozenset()

# Maximum backoff in minutes for consecutive patrol failures
_MAX_BACKOFF_MINUTES = 180


def _monitored_visibility(session, now):
    # Existing catalogs may include another shop's product owned by the same
    # user. Validate both shop owners without requiring identical shop IDs.
    owned_list_shop = session.query(Shop.id).filter(
        Shop.id == PriceList.shop_id, Shop.user_id == Product.user_id,
    ).correlate(PriceList, Product).exists()
    owned_product_shop = session.query(Shop.id).filter(
        Shop.id == Product.shop_id, Shop.user_id == Product.user_id,
    ).correlate(Product).exists()
    visible_list = session.query(PriceListItem.id).join(PriceList).filter(
        PriceListItem.product_id == Product.id,
        PriceListItem.visible.is_(True),
        PriceList.user_id == Product.user_id,
        PriceList.is_active.is_(True),
        or_(PriceList.unpublish_at == None, PriceList.unpublish_at > now),
        or_(PriceList.shop_id == None, owned_list_shop),
        or_(Product.shop_id == None, owned_product_shop),
    ).exists()
    return or_(Product.is_listed.isnot(False), visible_list)


class MonitorService:
    """
    Lightweight patrol service for efficient price/stock monitoring.
    Uses HTTP-only fetching for non-Mercari sites (no Chrome required).
    Shares a single WebDriver across Mercari items for speed.
    """
    
    # Site-specific patrol instances
    _patrols = {
        'mercari': MercariPatrol(),
        'yahoo': YahooPatrol(),
        'rakuma': RakumaPatrol(),
        'surugaya': SurugayaPatrol(),
        'offmall': OffmallPatrol(),
        'yahuoku': YahuokuPatrol(),
        'snkrdunk': SnkrdunkPatrol(),
        'recordcity': RecordCityPatrol(),
    }

    # Mercari-specific: track consecutive soft-sold counts per product to
    # implement hysteresis.  Only stored in memory — resets on restart,
    # which is the safe default (never persisting a stale count).
    # NOTE: Not thread-safe; relies on the single-threaded RQ worker model
    # used in production.  If concurrency is introduced, wrap with a Lock.
    _mercari_soft_sold_counts: dict[int, int] = {}

    # Number of consecutive soft-sold patrol results required before
    # the status is actually persisted as ``sold``.
    _MERCARI_SOFT_SOLD_THRESHOLD = 2
    
    @staticmethod
    def _apply_backoff(product, session_db, now=None):
        """Increment fail count and schedule the next patrol attempt."""
        now = now or utc_now()
        product.patrol_fail_count = (product.patrol_fail_count or 0) + 1
        backoff_minutes = min(product.patrol_fail_count * 15, _MAX_BACKOFF_MINUTES)
        product.last_patrolled_at = now
        product.next_patrol_at = now + timedelta(minutes=backoff_minutes)
        session_db.commit()

    @staticmethod
    def _pause_invalid_url(product, session_db, now=None):
        """Record a deterministic error once, preserving last verified data."""
        MonitorService._mercari_soft_sold_counts.pop(product.id, None)
        product.patrol_fail_count = (product.patrol_fail_count or 0) + 1
        product.last_patrolled_at = now or utc_now()
        product.next_patrol_at = None
        product.patrol_paused_reason = "invalid_url"
        product.patrol_paused_source_url = product.source_url
        session_db.commit()

    @staticmethod
    def _resume_valid_urls(session_db, before_commit=None):
        """Resume after URL correction or a newly supported route, without I/O.

        Revalidation is cheap and uses the current URL policy. Invalid paused
        rows do not occupy the batch limit or repeatedly emit failure events.
        A successful fetch, not this revalidation, clears the failure counter.
        """
        resumed = 0
        now = utc_now()
        paused = session_db.query(Product).filter(
            Product.site.in_(list(MonitorService._patrols)),
            Product.patrol_paused_reason == "invalid_url",
            Product.archived != True,
            _monitored_visibility(session_db, now),
            Product.deleted_at == None,
        )
        for product in paused.yield_per(200):
            if is_valid_detail_url(product.source_url, product.site):
                product.patrol_paused_reason = None
                product.patrol_paused_source_url = None
                product.next_patrol_at = None
                resumed += 1
        if resumed:
            if before_commit is not None:
                before_commit()
            session_db.commit()
        return resumed

    @staticmethod
    def check_stale_products(limit=15, *, fetcher=None, deadline=None,
                             cancelled=None, item_timeout=None, progress=None,
                             checkpoint_attempts=False, product_ids=None, ownership_guard=None,
                             session_factory=None, resource_guard=None):
        """Observe due listings, keeping database transactions out of fetch I/O.

        A supplied supervisor fetcher owns the hard browser-process deadline.
        Persistence uses short transactions, connection/statement limits and
        deadline/ownership checks; a synchronous database driver is not falsely
        represented as being hard-cancelled by a Python Future timeout.
        """
        logger.info("--- Starting Lightweight Patrol (Limit: %s) ---", limit)
        deadline = deadline if deadline is not None else time.monotonic() + patrol_batch_timeout()
        item_timeout = item_timeout if item_timeout is not None else patrol_item_timeout()
        session_factory = session_factory or create_isolated_session
        session_db = session_factory()
        summary = {
            "status": "started", "limit": limit, "eligible_count": 0,
            "selected_count": 0, "updated_count": 0, "successful_count": 0,
            "error_count": 0, "paused_count": 0, "resumed_count": 0,
            "attempted_count": 0, "unprocessed_count": 0, "discarded_count": 0,
            "inconclusive_count": 0,
            "site_counts": {}, "site_results": {},
        }
        site_results = summary["site_results"]
        snapshots = {}
        updated_count = error_count = successful_count = 0

        def progress_snapshot(event, **fields):
            if progress is None:
                return
            snapshot = dict(summary, updated_count=updated_count, error_count=error_count,
                            successful_count=successful_count)
            snapshot["unprocessed_count"] = snapshot["selected_count"] - (
                successful_count + error_count + snapshot["discarded_count"] + snapshot["inconclusive_count"])
            progress(dict(event=event, summary=snapshot, **fields))

        def check_active():
            if cancelled is not None and cancelled():
                raise PatrolCancelled("Patrol scheduler ownership was lost")
            check_deadline()
            if ownership_guard is not None:
                ownership_guard()
                check_deadline()

        def add_result(site, outcome, reason=None):
            counts = site_results.setdefault(
                site, {"successful_count": 0, "error_count": 0, "inconclusive_count": 0, "reason": None}
            )
            counts[outcome] += 1
            if reason and counts["reason"] is None:
                counts["reason"] = reason

        def load_current(product_id, site, url):
            # A listing may be deleted, paused or moved while the child fetches.
            # Accept only evidence for the current authorized listing identity.
            check_active()
            now = utc_now()
            product = session_db.query(Product).filter(
                Product.id == product_id, Product.archived != True,
                Product.deleted_at == None, _monitored_visibility(session_db, now),
            ).with_for_update(of=Product).one_or_none()
            if product is None or product.site != site or product.source_url != url:
                return None
            if product.patrol_paused_reason is not None:
                return None
            snapshot = snapshots[product_id]
            if any(getattr(product, field) != value for field, value in snapshot.items()):
                return None
            return product

        def bound_persistence():
            # The driver connect timeout is configured at engine construction.
            # Re-evaluate after connecting; SQL gets only the remaining budget.
            check_active()
            if session_db.get_bind().dialect.name == "postgresql":
                session_db.connection()
                check_active()
                seconds = max(0.001, deadline_for_item - time.monotonic())
                milliseconds = max(1, int(seconds * 1000))
                session_db.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                                   {"value": str(milliseconds)})
                session_db.execute(text("SELECT set_config('lock_timeout', :value, true)"),
                                   {"value": str(min(milliseconds, 5000))})
                check_active()

        try:
            with deadline_scope(deadline):
                check_active()
                now = utc_now()
                summary["resumed_count"] = MonitorService._resume_valid_urls(session_db, before_commit=check_active)
                patrol_cursor = func.coalesce(Product.last_patrolled_at, Product.updated_at, Product.created_at)
                eligible_products = session_db.query(Product).filter(
                    Product.site.in_(list(MonitorService._patrols.keys())),
                    Product.archived != True, _monitored_visibility(session_db, now),
                    Product.deleted_at == None, Product.patrol_paused_reason == None,
                    or_(Product.detail_fetch_state == None, Product.detail_fetch_state == "complete"),
                    or_(Product.next_patrol_at == None, Product.next_patrol_at <= now),
                )
                if product_ids is not None:
                    eligible_products = eligible_products.filter(Product.id.in_(product_ids))
                summary["eligible_count"] = eligible_products.count()
                selected = eligible_products.order_by(asc(patrol_cursor), asc(Product.id)).limit(limit).all()
                # Copy scalars before closing the selection transaction. ORM
                # instances are reloaded only after an accepted fetch result.
                products = [(product.id, product.site, product.source_url) for product in selected]
                snapshots = {
                    product.id: {field: getattr(product, field) for field in (
                        "user_id", "shop_id", "last_patrolled_at", "updated_at", "last_price", "last_status",
                    )}
                    for product in selected
                }
                summary["selected_count"] = len(products)
                summary["site_counts"] = {
                    site: sum(1 for _, product_site, _ in products if product_site == site)
                    for site in sorted({site for _, site, _ in products})
                }
                session_db.close()
                session_db = None
                progress_snapshot("selected", selected_ids=[product_id for product_id, _, _ in products])
                if not products:
                    summary["status"] = "no_products"
                    logger.info("No products to monitor.")
                    return summary

                for product_id, product_site, source_url in products:
                    check_active()
                    # Leave room for verified termination AND a bounded
                    # persistence/backoff transaction inside the item budget.
                    persistence_reserve = 10.0 if fetcher is not None else 0.0
                    if fetcher is not None and deadline - time.monotonic() <= 15.0 + persistence_reserve:
                        raise OperationDeadlineExceeded("Insufficient batch budget for another product")
                    deadline_for_item = min(deadline, time.monotonic() + item_timeout)
                    summary["attempted_count"] += 1
                    progress_snapshot("item_started", product_id=product_id, site=product_site,
                                      deadline=deadline_for_item)
                    failure_stage = "fetch_error"
                    product = None
                    try:
                        with deadline_scope(deadline_for_item):
                            check_active()
                            logger.info("Patrol: %s...", source_url[:50])
                            if not is_valid_detail_url(source_url, product_site):
                                session_db = session_factory()
                                bound_persistence()
                                product = load_current(product_id, product_site, source_url)
                                if product is None:
                                    summary["discarded_count"] += 1
                                    continue
                                check_active()
                                MonitorService._pause_invalid_url(product, session_db)
                                summary["paused_count"] += 1
                                error_count += 1
                                add_result(product_site, "error_count", "invalid_url")
                                continue

                            patrol = MonitorService._patrols.get(product_site)
                            if not patrol:
                                error_count += 1
                                add_result(product_site, "error_count", "unsupported_route")
                                continue
                            if checkpoint_attempts:
                                # A hard-killed attempt must not retain the
                                # oldest cursor and monopolize every batch.
                                # Price/status remain the last verified data.
                                progress_snapshot("phase", product_id=product_id, phase="attempt_checkpoint")
                                session_db = session_factory()
                                bound_persistence()
                                product = load_current(product_id, product_site, source_url)
                                if product is None:
                                    summary["discarded_count"] += 1
                                    continue
                                attempted_at = utc_now()
                                product.last_patrolled_at = attempted_at
                                product.next_patrol_at = attempted_at + timedelta(minutes=15)
                                check_active()
                                session_db.commit()
                                snapshots[product_id]["last_patrolled_at"] = attempted_at
                                session_db.close()
                                session_db = None
                                product = None
                            if fetcher is None:
                                progress_snapshot("phase", product_id=product_id, phase="fetch")
                                result = patrol.fetch(source_url)
                            else:
                                fetch = getattr(fetcher, "fetch", fetcher)
                                result = fetch(product_site, source_url,
                                               deadline=deadline_for_item - persistence_reserve,
                                               cancelled=cancelled)
                            if resource_guard is not None:
                                resource_guard()
                            check_active()
                            progress_snapshot("phase", product_id=product_id, phase="persistence")
                            session_db = session_factory()
                            bound_persistence()
                            product = load_current(product_id, product_site, source_url)
                            if product is None:
                                summary["discarded_count"] += 1
                                continue
                            if fetcher is not None:
                                report_patrol_result(product_site, source_url, result)
                                check_active()
                            normalized_status = normalize_status_for_persistence(result.status)
                            active_missing_price = (normalized_status == "on_sale" and result.price is None
                                                    and str(result.confidence or "").lower() == "low")
                            unknown_status = normalized_status == "unknown"
                            if not result.success or active_missing_price or unknown_status:
                                reason = result.error or result.reason or (
                                    "unknown patrol status" if unknown_status else "low-confidence active result")
                                logger.warning("Patrol failed: %s", reason)
                                failure_stage = "persistence_error"
                                check_active()
                                MonitorService._apply_backoff(product, session_db)
                                error_count += 1
                                add_result(product_site, "error_count", classify_scrape_failure(
                                    reason, default="unknown_status" if unknown_status else "missing_price"
                                    if active_missing_price else "invalid_result"))
                                continue

                            product.patrol_fail_count = 0
                            product.last_patrolled_at = utc_now()
                            product.next_patrol_at = None
                            failure_stage = "persistence_error"
                            if (product_site == "mercari" and normalized_status == "sold"
                                    and getattr(result, "evidence_strength", "hard") == "soft"):
                                count = min(MonitorService._MERCARI_SOFT_SOLD_THRESHOLD,
                                            MonitorService._mercari_soft_sold_counts.get(product_id, 0) + 1)
                                if count < MonitorService._MERCARI_SOFT_SOLD_THRESHOLD:
                                    logger.info("Mercari soft-sold deferred for product %s (%s/%s): %s",
                                                product_id, count, MonitorService._MERCARI_SOFT_SOLD_THRESHOLD,
                                                result.reason or "")
                                    product.updated_at = product.last_patrolled_at
                                    check_active()
                                    session_db.commit()
                                    MonitorService._mercari_soft_sold_counts[product_id] = count
                                    add_result(product_site, "inconclusive_count", "inconclusive")
                                    summary["inconclusive_count"] += 1
                                    continue
                            else:
                                MonitorService._mercari_soft_sold_counts.pop(product_id, None)

                            changes = MonitorService._apply_patrol_result(session_db, product, result)
                            if changes > 0 and product_has_pricing_config(product):
                                update_product_selling_price(product_id, session=session_db)
                            product.updated_at = product.last_patrolled_at or utc_now()
                            check_active()
                            progress_snapshot("phase", product_id=product_id, phase="commit")
                            session_db.commit()
                            if (product_site == "mercari" and normalized_status == "sold"
                                    and getattr(result, "evidence_strength", "hard") == "soft"):
                                MonitorService._mercari_soft_sold_counts[product_id] = count
                            if changes > 0:
                                logger.info("Updated %s: %s changes", product_id, changes)
                                updated_count += 1
                            try:
                                observed_price = normalize_price_for_persistence(result.price)
                            except (TypeError, ValueError, OverflowError):
                                observed_price = None
                            if normalized_status == "on_sale" and (observed_price is None or observed_price <= 0):
                                error_count += 1
                                add_result(product_site, "error_count", "missing_price")
                            else:
                                successful_count += 1
                                add_result(product_site, "successful_count")
                    except (PatrolCancelled, PatrolSupervisorQuarantined):
                        if session_db is not None:
                            session_db.rollback()
                        raise
                    except Exception as exc:
                        logger.error("Error checking product %s: %s", product_id, type(exc).__name__)
                        if session_db is not None:
                            session_db.rollback()
                        # Fetch timeouts leave their termination reserve for
                        # this backoff. A wholly exhausted deadline gets no
                        # extra unbounded transaction tacked onto its budget.
                        try:
                            with deadline_scope(deadline_for_item):
                                check_active()
                                if session_db is None:
                                    session_db = session_factory()
                                    bound_persistence()
                                product = load_current(product_id, product_site, source_url)
                                if product is not None:
                                    check_active()
                                    MonitorService._apply_backoff(product, session_db)
                        except (OperationDeadlineExceeded, PatrolCancelled):
                            if session_db is not None:
                                session_db.rollback()
                        except Exception:
                            logger.exception("Backoff failed for product %s", product_id)
                        error_count += 1
                        add_result(product_site, "error_count", classify_scrape_failure(exc, default=failure_stage))
                    finally:
                        if session_db is not None:
                            session_db.close()
                            session_db = None
                        progress_snapshot("item_finished", product_id=product_id)
                summary["status"] = "completed"
        except (OperationDeadlineExceeded, PatrolCancelled, PatrolSupervisorQuarantined) as exc:
            summary["status"] = ("deadline_exceeded" if isinstance(exc, OperationDeadlineExceeded)
                                 else "cancelled" if isinstance(exc, PatrolCancelled) else "quarantined")
            summary["abort_reason"] = type(exc).__name__
            logger.warning("Patrol interrupted: %s", summary["status"])
        except Exception as exc:
            summary["status"] = "fatal_error"
            summary["fatal_error"] = type(exc).__name__
            logger.error("Patrol fatal error: %s", type(exc).__name__)
            if session_db is not None:
                session_db.rollback()
        finally:
            summary["updated_count"] = updated_count
            summary["successful_count"] = successful_count
            summary["error_count"] = error_count
            summary["unprocessed_count"] = summary["selected_count"] - (
                successful_count + error_count + summary["discarded_count"] + summary["inconclusive_count"])
            if session_db is not None:
                session_db.close()
            for site, counts in site_results.items():
                outcome = ("failure" if counts["error_count"] else "no_observations"
                           if counts["inconclusive_count"] else "success")
                record_observation_safely(
                    site=site, route="patrol", outcome=outcome, reason=counts["reason"],
                    success_count=counts["successful_count"], error_count=counts["error_count"],
                )
            logger.info("Patrol complete: %s updated, %s errors, %s unprocessed (%s)",
                        updated_count, error_count, summary["unprocessed_count"], summary["status"])
            logger.info("--- Patrol Finished ---")
        return summary

    @staticmethod
    def _apply_patrol_result(session_db, product, result) -> int:
        """
        Apply patrol result to product and variants.
        Returns number of changes made.
        """
        changes = 0
        normalized_status = normalize_status_for_persistence(result.status)
        
        # Update price if changed
        if (
            result.price is not None
            and str(result.confidence or "").lower() == "high"
            and result.price != product.last_price
        ):
            product.last_price = result.price
            changes += 1
        
        # Update status if changed
        if normalized_status and normalized_status != "unknown" and normalized_status != product.last_status:
            product.last_status = normalized_status
            changes += 1
        
        existing_variants = session_db.query(Variant).filter_by(
            product_id=product.id
        ).all()

        if normalized_status in {"sold", "deleted"}:
            for existing in existing_variants:
                if existing.inventory_qty != 0:
                    existing.inventory_qty = 0
                    changes += 1
        elif normalized_status == "on_sale" and not result.variants:
            for existing in existing_variants:
                if existing.option1_value == "Default Title" and (existing.inventory_qty or 0) == 0:
                    existing.inventory_qty = 1
                    changes += 1

        # Update variant stock if available
        if result.variants:
            # Match variants by name/option and update stock
            for patrol_var in result.variants:
                var_name = patrol_var.get("name", "")
                var_stock = patrol_var.get("stock", 0)
                var_price = patrol_var.get("price")
                
                for existing in existing_variants:
                    # Match by option1_value or combined name
                    existing_name = existing.option1_value or ""
                    if existing.option2_value:
                        existing_name += f" / {existing.option2_value}"
                    
                    if var_name in existing_name or existing_name in var_name:
                        if existing.inventory_qty != var_stock:
                            existing.inventory_qty = var_stock
                            changes += 1
                        if (
                            var_price
                            and str(result.confidence or "").lower() == "high"
                            and existing.price != var_price
                        ):
                            existing.price = var_price
                            changes += 1
                        break
        
        return changes
