"""add fencing lease for translation workers

Revision ID: 20260729_0014
Revises: 20260729_0013
Create Date: 2026-07-29 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260729_0014"
down_revision = "20260729_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "translation_suggestions" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"]
        for column in inspector.get_columns("translation_suggestions")
    }
    with op.batch_alter_table("translation_suggestions") as batch_op:
        if "worker_token" not in existing_columns:
            batch_op.add_column(sa.Column("worker_token", sa.String(64), nullable=True))
        if "lease_expires_at" not in existing_columns:
            batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))

    # A pre-migration `running` row has no owner token.  Make its lease
    # immediately reclaimable instead of leaving it stuck indefinitely.
    suggestions = sa.table(
        "translation_suggestions",
        sa.column("status", sa.String(16)),
        sa.column("updated_at", sa.DateTime()),
        sa.column("worker_token", sa.String(64)),
        sa.column("lease_expires_at", sa.DateTime()),
    )
    op.execute(
        suggestions.update()
        .where(
            sa.and_(
                suggestions.c.status == "running",
                suggestions.c.worker_token.is_(None),
                suggestions.c.lease_expires_at.is_(None),
            )
        )
        .values(lease_expires_at=suggestions.c.updated_at)
    )

    inspector = sa.inspect(bind)
    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes("translation_suggestions")
        if index.get("name")
    }
    if "ix_translation_suggestions_lease_expires_at" not in existing_indexes:
        op.create_index(
            "ix_translation_suggestions_lease_expires_at",
            "translation_suggestions",
            ["lease_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "translation_suggestions" not in set(inspector.get_table_names()):
        return

    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes("translation_suggestions")
        if index.get("name")
    }
    if "ix_translation_suggestions_lease_expires_at" in existing_indexes:
        op.drop_index(
            "ix_translation_suggestions_lease_expires_at",
            table_name="translation_suggestions",
        )

    existing_columns = {
        column["name"]
        for column in inspector.get_columns("translation_suggestions")
    }
    with op.batch_alter_table("translation_suggestions") as batch_op:
        if "lease_expires_at" in existing_columns:
            batch_op.drop_column("lease_expires_at")
        if "worker_token" in existing_columns:
            batch_op.drop_column("worker_token")
