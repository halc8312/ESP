"""add shops.instagram_username and price_lists.shipping_note

Backs the public catalog's contact button and its shipping estimate. Both are
optional; an empty value means the catalog shows nothing extra.

Revision ID: 20260808_0018
Revises: 20260807_0017
Create Date: 2026-08-08 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260808_0018"
down_revision = "20260807_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "shops" in table_names:
        columns = {column["name"] for column in inspector.get_columns("shops")}
        if "instagram_username" not in columns:
            with op.batch_alter_table("shops") as batch_op:
                batch_op.add_column(
                    sa.Column("instagram_username", sa.String(length=64), nullable=True)
                )

    if "price_lists" in table_names:
        columns = {column["name"] for column in inspector.get_columns("price_lists")}
        if "shipping_note" not in columns:
            with op.batch_alter_table("price_lists") as batch_op:
                batch_op.add_column(
                    sa.Column("shipping_note", sa.String(length=200), nullable=True)
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "price_lists" in table_names:
        columns = {column["name"] for column in inspector.get_columns("price_lists")}
        if "shipping_note" in columns:
            with op.batch_alter_table("price_lists") as batch_op:
                batch_op.drop_column("shipping_note")

    if "shops" in table_names:
        columns = {column["name"] for column in inspector.get_columns("shops")}
        if "instagram_username" in columns:
            with op.batch_alter_table("shops") as batch_op:
                batch_op.drop_column("instagram_username")
