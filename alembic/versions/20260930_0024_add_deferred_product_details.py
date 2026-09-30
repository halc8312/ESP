"""Add opt-in deferred details without changing legacy products.

Revision ID: 20260930_0024
Revises: 20260929_0023
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260930_0024"
down_revision = "20260929_0023"
branch_labels = None
depends_on = None


def _columns():
    return (
        sa.Column("detail_fetch_state", sa.String(16), nullable=True),
        sa.Column("detail_job_id", sa.String(64), nullable=True),
        sa.Column("detail_source_url", sa.String(), nullable=True),
        sa.Column("detail_scope_key", sa.String(64), nullable=True),
        sa.Column("detail_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("detail_retry_at", sa.DateTime(), nullable=True),
        sa.Column("detail_fail_count", sa.Integer(), nullable=True),
        sa.Column("detail_error_code", sa.String(64), nullable=True),
        sa.Column("detail_translate_requested", sa.Boolean(), nullable=True),
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "products" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("products")}
    with op.batch_alter_table("products") as batch:
        for column in _columns():
            if column.name not in existing:
                batch.add_column(column)
    inspector = sa.inspect(op.get_bind())
    if "ix_products_detail_fetch_state" not in {i["name"] for i in inspector.get_indexes("products")}:
        op.create_index("ix_products_detail_fetch_state", "products", ["detail_fetch_state"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "products" not in inspector.get_table_names():
        return
    if "ix_products_detail_fetch_state" in {i["name"] for i in inspector.get_indexes("products")}:
        op.drop_index("ix_products_detail_fetch_state", table_name="products")
    existing = {column["name"] for column in inspector.get_columns("products")}
    with op.batch_alter_table("products") as batch:
        for column in reversed(_columns()):
            if column.name in existing:
                batch.drop_column(column.name)
