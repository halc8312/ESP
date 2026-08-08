"""add users.role and users.last_login_at

Backs the school office's student-activity dashboard: role decides who can see
it, last_login_at is the signal for "this student has gone quiet".

Revision ID: 20260807_0017
Revises: 20260807_0016
Create Date: 2026-08-07 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260807_0017"
down_revision = "20260807_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in set(inspector.get_table_names()):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}

    if "role" not in existing_columns:
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "role",
                    sa.String(length=16),
                    nullable=False,
                    server_default="student",
                )
            )

    if "last_login_at" not in existing_columns:
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in set(inspector.get_table_names()):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}

    with op.batch_alter_table("users") as batch_op:
        if "last_login_at" in existing_columns:
            batch_op.drop_column("last_login_at")
        if "role" in existing_columns:
            batch_op.drop_column("role")
