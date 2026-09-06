"""Retain customer inquiries and their public product snapshots.

Revision ID: 20260906_0022
Revises: 20260905_0021
"""
import sqlalchemy as sa
from alembic import op

revision = "20260906_0022"
down_revision = "20260905_0021"
branch_labels = None
depends_on = None


def upgrade():
    # create_all-based legacy bootstraps can already contain the new tables.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "catalog_requests" not in existing:
        op.create_table(
            "catalog_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("reference", sa.String(20), nullable=False, unique=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("pricelist_id", sa.Integer(), sa.ForeignKey("price_lists.id", ondelete="SET NULL")),
            sa.Column("shop_id", sa.Integer(), sa.ForeignKey("shops.id", ondelete="SET NULL")),
            sa.Column("pricelist_name", sa.String(), nullable=False),
            sa.Column("shop_name", sa.String()),
            sa.Column("buyer_instagram", sa.String(30), nullable=False),
            sa.Column("buyer_name", sa.String(100)),
            sa.Column("message", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("viewed_at", sa.DateTime()),
            sa.Column("submission_key", sa.String(32), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.UniqueConstraint("user_id", "submission_key", name="uq_catalog_request_submission"),
        )
        op.create_index("ix_catalog_request_owner_created", "catalog_requests", ["user_id", "created_at", "id"])
        op.create_index("ix_catalog_request_owner_unread", "catalog_requests", ["user_id", "viewed_at", "created_at"])
    if "catalog_request_items" not in existing:
        op.create_table(
            "catalog_request_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("request_id", sa.Integer(), sa.ForeignKey("catalog_requests.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("title_snapshot", sa.String(), nullable=False),
            sa.Column("price_jpy_snapshot", sa.Integer()),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.CheckConstraint("quantity >= 1 AND quantity <= 99", name="ck_catalog_request_quantity"),
        )
        op.create_index("ix_catalog_request_item_request", "catalog_request_items", ["request_id"])


def downgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("catalog_request_items", "catalog_requests"):
        if table in existing:
            op.drop_table(table)
