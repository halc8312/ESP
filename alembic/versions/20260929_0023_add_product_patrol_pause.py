"""Persist patrol pauses for deterministic URL errors.

Revision ID: 20260929_0023
Revises: 20260906_0022
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260929_0023"
down_revision = "20260906_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "products" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("products")}
    with op.batch_alter_table("products") as batch:
        if "patrol_paused_reason" not in existing:
            batch.add_column(sa.Column("patrol_paused_reason", sa.String(32), nullable=True))
        if "patrol_paused_source_url" not in existing:
            batch.add_column(sa.Column("patrol_paused_source_url", sa.String(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "products" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("products")}
    with op.batch_alter_table("products") as batch:
        for name in ("patrol_paused_source_url", "patrol_paused_reason"):
            if name in existing:
                batch.drop_column(name)
