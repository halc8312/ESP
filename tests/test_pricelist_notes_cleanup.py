"""
Clearing the literal word "None" left in price list notes.

The old edit form printed the notes attribute straight into the editor, so a
list with no notes showed "None" and saving stored it. The live site had one
list carrying ``<p>None</p>``, which the public catalog rendered as a paragraph
reading "None" to the customer. The migration clears that residue; these tests
pin what it does and does not touch.
"""
import importlib.util
from pathlib import Path

import pytest


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260812_0019_clear_literal_none_pricelist_notes.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_notes_cleanup", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


class TestWhatCountsAsEmpty:
    @pytest.mark.parametrize(
        "stored",
        [
            "None",
            "  None  ",
            "<p>None</p>",
            "<p>None</p>\n",
            "<div><p> None </p></div>",
            "<p>&nbsp;None&nbsp;</p>",
        ],
    )
    def test_a_note_that_is_only_that_word_is_treated_as_empty(self, stored):
        assert migration._is_only_the_word_none(stored) is True

    @pytest.mark.parametrize(
        "stored",
        [
            "None of these are in stock",
            "<p>Nonetheless, thank you!</p>",
            "在庫はNoneです",
            "<p>Thank you for your interest!</p>",
            "",
            "<p></p>",
        ],
    )
    def test_real_notes_are_left_alone(self, stored):
        # Including notes that merely contain the word.
        assert migration._is_only_the_word_none(stored) is False


class TestTheMigration:
    def test_only_the_placeholder_rows_are_cleared(self, db_session):
        from models import PriceList, User
        from time_utils import utc_now

        user = User(username="notes_cleanup_user", last_login_at=utc_now())
        user.set_password("testpassword")
        db_session.add(user)
        db_session.commit()

        placeholder = PriceList(
            user_id=user.id, name="Placeholder", token="notes-none", notes="<p>None</p>"
        )
        real = PriceList(
            user_id=user.id, name="Real", token="notes-real", notes="<p>Thank you!</p>"
        )
        empty = PriceList(user_id=user.id, name="Empty", token="notes-empty", notes=None)
        db_session.add_all([placeholder, real, empty])
        db_session.commit()

        import alembic.op as alembic_op

        class _Op:
            def __init__(self, bind):
                self._bind = bind

            def get_bind(self):
                return self._bind

        original_get_bind = migration.op.get_bind
        migration.op = _Op(db_session.connection())
        try:
            migration.upgrade()
        finally:
            migration.op = alembic_op
            assert original_get_bind is alembic_op.get_bind

        db_session.expire_all()
        assert db_session.get(PriceList, placeholder.id).notes is None
        assert db_session.get(PriceList, real.id).notes == "<p>Thank you!</p>"
        assert db_session.get(PriceList, empty.id).notes is None
