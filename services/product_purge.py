"""Permanent Product deletion, after the caller has locked an eligible trash row.

Keep the NO ACTION foreign keys: they remain a final guard against new or
unhandled references. Owned ephemeral children are removed in the same
transaction; customer request snapshots deliberately survive.
"""
from sqlalchemy import or_

from models import (
    CatalogRequestItem,
    ImageProcessingJob,
    PriceList,
    PriceListItem,
    Product,
    ProductSnapshot,
    ProductThumbnailJob,
    TranslationSuggestion,
    Variant,
)


class ProductPurgeOwnershipError(RuntimeError):
    """A child references this product from a different tenant; fail closed."""


def purge_locked_trashed_product(session, product: Product) -> None:
    """Delete a locked trash product without leaving restrictive FK children.

    The caller must hold the Product write lock and revalidate ownership and
    trash eligibility in this transaction. It must also refuse a non-null
    thumbnail ``job_id`` or queued/running background-removal job: physical
    uploads must finish before their durable reservations disappear. Never
    call this helper on an active Product.
    """
    if product.deleted_at is None:
        raise ValueError("permanent deletion requires an eligible trash product")

    for child in (TranslationSuggestion, ImageProcessingJob, ProductThumbnailJob):
        foreign = session.query(child.product_id).filter(
            child.product_id == product.id,
            child.user_id != product.user_id,
        ).first()
        if foreign is not None:
            raise ProductPurgeOwnershipError("product dependent owner mismatch")

    foreign_listing = (
        session.query(PriceListItem.id)
        .outerjoin(PriceList, PriceList.id == PriceListItem.price_list_id)
        .filter(
            PriceListItem.product_id == product.id,
            or_(PriceList.user_id != product.user_id, PriceList.id.is_(None)),
        )
        .first()
    )
    if foreign_listing is not None:
        raise ProductPurgeOwnershipError("product pricelist owner mismatch")

    # No commits here. The caller commits (or rolls back) the complete purge.
    # Worker results are UPDATE-only CAS operations, so after deletion an old
    # translation worker updates zero rows; it cannot recreate a suggestion.
    for child in (TranslationSuggestion, ImageProcessingJob, PriceListItem,
                  ProductThumbnailJob, Variant, ProductSnapshot):
        session.query(child).filter(child.product_id == product.id).delete(
            synchronize_session=False,
        )

    # The schema already uses SET NULL. Do this explicitly as well for legacy
    # SQLite installations with FK enforcement disabled; retain all snapshots.
    session.query(CatalogRequestItem).filter(
        CatalogRequestItem.product_id == product.id,
    ).update({CatalogRequestItem.product_id: None}, synchronize_session=False)
    deleted_count = session.query(Product).filter(
        Product.id == product.id,
        Product.user_id == product.user_id,
        Product.deleted_at.is_not(None),
    ).delete(synchronize_session=False)
    if deleted_count != 1:
        # Never commit child cleanup after restore/ownership drift. The route
        # rolls the entire transaction back on this invariant failure.
        raise RuntimeError("locked trash product changed during purge")
