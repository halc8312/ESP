"""add users.email and users.suspended_at

The school office registers students, so it holds their email address from the
application form; the tool had nowhere to keep it. Suspension covers a break
in attendance or unpaid fees as well as leaving, so it is a switch that can go
back rather than a deletion: nothing is removed, and resuming restores the
account and its published lists as they were.

Revision ID: 20260827_0020
Revises: 20260812_0019
Create Date: 2026-08-27 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260827_0020"
down_revision = "20260812_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    with op.batch_alter_table("users") as batch_op:
        if "email" not in columns:
            batch_op.add_column(sa.Column("email", sa.String(length=255), nullable=True))
        if "suspended_at" not in columns:
            batch_op.add_column(sa.Column("suspended_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    with op.batch_alter_table("users") as batch_op:
        if "suspended_at" in columns:
            batch_op.drop_column("suspended_at")
        if "email" in columns:
            batch_op.drop_column("email")
