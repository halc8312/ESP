"""clear price_lists.notes rows whose only content is the word "None"

The edit form used to print ``{{ pricelist.notes }}`` straight into the notes
editor, so a list with no notes showed the literal string ``None`` and saving
the form stored it. On the public catalog that reads as a paragraph saying
"None" to the customer. Found on the live site: one list carried
``<p>None</p>``.

The template no longer produces it, so this clears what the old one left
behind. Only a value that is *nothing but* that word is touched — notes that
merely contain it are left alone.

Revision ID: 20260812_0019
Revises: 20260808_0018
Create Date: 2026-08-12 00:00:00
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op


revision = "20260812_0019"
down_revision = "20260808_0018"
branch_labels = None
depends_on = None


_TAG = re.compile(r"<[^>]+>")
_NBSP = " "


def _is_only_the_word_none(value: str) -> bool:
    text = _TAG.sub("", value).replace("&nbsp;", " ").replace(_NBSP, " ")
    return text.strip() == "None"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "price_lists" not in set(inspector.get_table_names()):
        return
    # Older databases reach this revision before the column exists.
    if "notes" not in {column["name"] for column in inspector.get_columns("price_lists")}:
        return

    # The database narrows the candidates; Python decides, because "contains
    # the word" and "is only the word" are not the same thing.
    rows = bind.execute(
        sa.text("SELECT id, notes FROM price_lists WHERE notes LIKE '%None%'")
    ).fetchall()
    stale_ids = [row[0] for row in rows if row[1] and _is_only_the_word_none(row[1])]
    if not stale_ids:
        return

    statement = sa.text("UPDATE price_lists SET notes = NULL WHERE id IN :ids").bindparams(
        sa.bindparam("ids", expanding=True)
    )
    bind.execute(statement, {"ids": stale_ids})


def downgrade() -> None:
    # The cleared value carried no information, so there is nothing to restore.
    pass
