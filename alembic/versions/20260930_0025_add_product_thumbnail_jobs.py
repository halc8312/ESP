"""Durable image-only demands; no legacy product/image rewrites.

Revision ID: 20260930_0025
Revises: 20260930_0024
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260930_0025"
down_revision = "20260930_0024"
branch_labels = None
depends_on = None


def upgrade():
    if "product_thumbnail_jobs" in sa.inspect(op.get_bind()).get_table_names():
        if "batch_user_id" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("product_thumbnail_jobs")}:
            op.add_column("product_thumbnail_jobs", sa.Column("batch_user_id", sa.Integer(), nullable=True))
        return
    op.create_table(
        "product_thumbnail_jobs",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("shop_id", sa.Integer(), sa.ForeignKey("shops.id"), nullable=True),
        sa.Column("product_source_url", sa.String(), nullable=False),
        sa.Column("source_snapshot_id", sa.Integer(), sa.ForeignKey("product_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_image_url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("job_id", sa.String(64), nullable=True),
        sa.Column("batch_user_id", sa.Integer(), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("retry_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("managed_image_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_product_thumbnail_jobs_user_id", "product_thumbnail_jobs", ["user_id"])
    op.create_index("ix_product_thumbnail_jobs_state_retry", "product_thumbnail_jobs", ["state", "retry_at"])


def downgrade():
    if "product_thumbnail_jobs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("product_thumbnail_jobs")
