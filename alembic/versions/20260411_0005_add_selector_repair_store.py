"""add selector repair store tables

Revision ID: 20260411_0005
Revises: 20260411_0004
Create Date: 2026-04-11 00:05:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260411_0005"
down_revision = "20260411_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "selector_repair_candidates" not in existing_tables:
        op.create_table(
            "selector_repair_candidates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site", sa.String(length=32), nullable=False),
            sa.Column("page_type", sa.String(length=32), nullable=False),
            sa.Column("field", sa.String(length=64), nullable=False),
            sa.Column("parser", sa.String(length=32), nullable=False),
            sa.Column("proposed_selector", sa.Text(), nullable=False),
            sa.Column("source_selector", sa.Text(), nullable=True),
            sa.Column("score", sa.Integer(), nullable=True),
            sa.Column("page_state", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("details_payload", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_selector_repair_candidates_lookup",
            "selector_repair_candidates",
            ["site", "page_type", "field", "status", "created_at"],
            unique=False,
        )

    existing_tables = set(inspector.get_table_names())
    if "selector_active_rule_sets" not in existing_tables:
        op.create_table(
            "selector_active_rule_sets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site", sa.String(length=32), nullable=False),
            sa.Column("page_type", sa.String(length=32), nullable=False),
            sa.Column("field", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("selectors_payload", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("source_candidate_id", sa.Integer(), sa.ForeignKey("selector_repair_candidates.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("activated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("site", "page_type", "field", "version", name="uq_selector_active_rule_sets_version"),
        )
        op.create_index(
            "ix_selector_active_rule_sets_lookup",
            "selector_active_rule_sets",
            ["site", "page_type", "field", "is_active", "version"],
            unique=False,
        )
        if bind.dialect.name == "postgresql":
            op.create_index(
                "uq_selector_active_rule_sets_one_active",
                "selector_active_rule_sets",
                ["site", "page_type", "field"],
                unique=True,
                postgresql_where=sa.text("is_active"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "selector_active_rule_sets" in existing_tables:
        existing_indexes = {index["name"] for index in inspector.get_indexes("selector_active_rule_sets")}
        if "uq_selector_active_rule_sets_one_active" in existing_indexes:
            op.drop_index("uq_selector_active_rule_sets_one_active", table_name="selector_active_rule_sets")
        if "ix_selector_active_rule_sets_lookup" in existing_indexes:
            op.drop_index("ix_selector_active_rule_sets_lookup", table_name="selector_active_rule_sets")
        op.drop_table("selector_active_rule_sets")

    existing_tables = set(inspector.get_table_names())
    if "selector_repair_candidates" in existing_tables:
        existing_indexes = {index["name"] for index in inspector.get_indexes("selector_repair_candidates")}
        if "ix_selector_repair_candidates_lookup" in existing_indexes:
            op.drop_index("ix_selector_repair_candidates_lookup", table_name="selector_repair_candidates")
        op.drop_table("selector_repair_candidates")
