"""add translation_suggestions.auto_apply and users.default_pricing_rule_id

Revision ID: 20260610_0010
Revises: 20260610_0009
Create Date: 2026-06-10 09:30:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260610_0010"
down_revision = "20260610_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "translation_suggestions" in table_names:
        existing = {c["name"] for c in inspector.get_columns("translation_suggestions")}
        if "auto_apply" not in existing:
            op.execute(sa.text(
                "ALTER TABLE translation_suggestions ADD COLUMN auto_apply BOOLEAN DEFAULT FALSE"
            ))

    if "users" in table_names:
        existing = {c["name"] for c in inspector.get_columns("users")}
        if "default_pricing_rule_id" not in existing:
            op.execute(sa.text(
                "ALTER TABLE users ADD COLUMN default_pricing_rule_id INTEGER"
            ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "users" in table_names:
        existing = {c["name"] for c in inspector.get_columns("users")}
        if "default_pricing_rule_id" in existing:
            with op.batch_alter_table("users") as batch_op:
                batch_op.drop_column("default_pricing_rule_id")

    if "translation_suggestions" in table_names:
        existing = {c["name"] for c in inspector.get_columns("translation_suggestions")}
        if "auto_apply" in existing:
            with op.batch_alter_table("translation_suggestions") as batch_op:
                batch_op.drop_column("auto_apply")
