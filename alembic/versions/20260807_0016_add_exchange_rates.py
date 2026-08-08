"""add exchange_rates and users.exchange_rate_margin

Stores the daily foreign exchange refresh used by the public catalog, plus the
operator's safety margin for foreign-currency display.

Revision ID: 20260807_0016
Revises: 20260807_0015
Create Date: 2026-08-07 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260807_0016"
down_revision = "20260807_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "exchange_rates" not in table_names:
        op.create_table(
            "exchange_rates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("quote_currency", sa.String(length=3), nullable=False),
            sa.Column("jpy_per_unit", sa.Float(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_exchange_rates_quote_currency",
            "exchange_rates",
            ["quote_currency"],
            unique=True,
        )

    if "users" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "exchange_rate_margin" not in user_columns:
            with op.batch_alter_table("users") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "exchange_rate_margin",
                        sa.Integer(),
                        nullable=False,
                        server_default="0",
                    )
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "users" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "exchange_rate_margin" in user_columns:
            with op.batch_alter_table("users") as batch_op:
                batch_op.drop_column("exchange_rate_margin")

    if "exchange_rates" in table_names:
        op.drop_index("ix_exchange_rates_quote_currency", table_name="exchange_rates")
        op.drop_table("exchange_rates")
