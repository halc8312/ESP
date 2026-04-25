"""add user scope to description templates

Revision ID: 20260422_0006
Revises: 20260420_0007
Create Date: 2026-04-22 00:06:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260422_0006"
down_revision = "20260420_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "description_templates" not in existing_tables:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("description_templates")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("description_templates")}
    existing_unique_constraints = [
        constraint
        for constraint in inspector.get_unique_constraints("description_templates")
        if constraint.get("name")
    ]
    legacy_name_unique_constraints = [
        str(constraint["name"])
        for constraint in existing_unique_constraints
        if list(constraint.get("column_names") or []) == ["name"]
    ]
    scoped_name_unique_present = any(
        list(constraint.get("column_names") or []) == ["user_id", "name"]
        for constraint in existing_unique_constraints
    )
    user_id_present = "user_id" in existing_columns

    with op.batch_alter_table("description_templates") as batch_op:
        if not user_id_present:
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_description_templates_user_id_users",
                "users",
                ["user_id"],
                ["id"],
            )
            user_id_present = True
        if "ix_description_templates_user_id" not in existing_indexes:
            batch_op.create_index("ix_description_templates_user_id", ["user_id"], unique=False)
        for constraint_name in legacy_name_unique_constraints:
            batch_op.drop_constraint(constraint_name, type_="unique")
        if not scoped_name_unique_present:
            batch_op.create_unique_constraint(
                "uq_description_templates_user_name",
                ["user_id", "name"],
            )

    if user_id_present and "users" in existing_tables:
        user_ids = list(bind.execute(sa.text("SELECT id FROM users ORDER BY id")).scalars())
        if len(user_ids) == 1:
            bind.execute(
                sa.text(
                    "UPDATE description_templates "
                    "SET user_id = :user_id "
                    "WHERE user_id IS NULL"
                ),
                {"user_id": user_ids[0]},
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "description_templates" not in existing_tables:
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("description_templates")}
    if "ix_description_templates_user_id" in existing_indexes:
        with op.batch_alter_table("description_templates") as batch_op:
            batch_op.drop_index("ix_description_templates_user_id")

    # Leave the additive column in place for compatibility with older rows.
