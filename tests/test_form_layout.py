"""
Wide-screen form layout.

The width change left short input fields stacked one per row with a lot of
empty space beside them. These assert the grid wrappers are present, and that
the price list edit form does not put the literal string "None" in the notes
box for a list that has no notes.
"""
from models import PriceList, User


def _login(client, db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


class TestFieldGrids:
    def test_pricing_rule_fields_share_a_row(self, client, db_session):
        _login(client, db_session, "grid_pricing_user")

        html = client.get("/pricing").get_data(as_text=True)

        assert "field-grid field-grid--3" in html

    def test_password_fields_share_a_row(self, client, db_session):
        _login(client, db_session, "grid_account_user")

        html = client.get("/account").get_data(as_text=True)

        assert "field-grid field-grid--3" in html

    def test_price_list_choice_cards_sit_side_by_side(self, client, db_session):
        _login(client, db_session, "grid_pricelist_user")

        html = client.get("/pricelists/create").get_data(as_text=True)

        assert "choice-grid" in html
        assert "field-grid field-grid--2" in html


class TestEmptyNotes:
    def test_a_list_without_notes_gets_an_empty_box(self, client, db_session):
        user = _login(client, db_session, "empty_notes_user")
        pricelist = PriceList(
            user_id=user.id,
            name="No Notes",
            token="empty-notes-token",
            is_active=True,
        )
        db_session.add(pricelist)
        db_session.commit()

        html = client.get(f"/pricelists/{pricelist.id}/edit").get_data(as_text=True)

        assert ">None</textarea>" not in html
        assert 'name="notes" rows="3"' in html
