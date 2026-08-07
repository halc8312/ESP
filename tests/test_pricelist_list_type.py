"""
Permanent (main) vs time-limited price lists.

Students run two kinds of list: the main one customers always see, and
temporary ones that get closed once their window passes. The list type keeps
the two apart on the index so a closed temporary list is never mistaken for the
main list going quiet.
"""
from datetime import timedelta

from models import PriceList, User
from time_utils import utc_now
from routes.pricelist import _describe_remaining_time, _normalize_list_type


def _create_user(db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, user):
    return client.post(
        "/login",
        data={"username": user.username, "password": "testpassword"},
    )


def _form(**overrides):
    values = {
        "name": "Some List",
        "notes": "",
        "currency_rate": "150",
        "layout": "grid",
        "theme": "dark",
        "shop_id": "",
        "is_active": "on",
        "unpublish_at": "",
        "list_type": "permanent",
    }
    values.update(overrides)
    return values


class TestListTypeNormalisation:
    def test_known_types_pass_through(self):
        assert _normalize_list_type("permanent") == "permanent"
        assert _normalize_list_type("limited") == "limited"

    def test_unknown_or_missing_falls_back_to_permanent(self):
        assert _normalize_list_type(None) == "permanent"
        assert _normalize_list_type("") == "permanent"
        assert _normalize_list_type("something-else") == "permanent"


class TestRemainingTimeLabel:
    def test_no_deadline_has_no_label(self):
        assert _describe_remaining_time(None) is None

    def test_a_passed_deadline_has_no_label(self):
        now = utc_now()
        assert _describe_remaining_time(now - timedelta(hours=1), now) is None

    def test_counts_down_in_the_largest_useful_unit(self):
        now = utc_now()
        assert _describe_remaining_time(now + timedelta(days=2, hours=3), now) == "あと2日"
        assert _describe_remaining_time(now + timedelta(hours=5), now) == "あと5時間"
        assert _describe_remaining_time(now + timedelta(minutes=20), now) == "あと20分"
        # Anything still in the future reads as at least a minute, never "あと0分".
        assert _describe_remaining_time(now + timedelta(seconds=30), now) == "あと1分"


class TestListTypePersistence:
    def test_create_stores_the_chosen_list_type(self, client, db_session):
        user = _create_user(db_session, "list_type_create_user")
        _login(client, user)

        client.post(
            "/pricelists/create",
            data=_form(name="Limited Drop", list_type="limited"),
            follow_redirects=True,
        )

        created = db_session.query(PriceList).filter_by(user_id=user.id).one()
        assert created.list_type == "limited"

    def test_create_defaults_to_permanent(self, client, db_session):
        user = _create_user(db_session, "list_type_default_user")
        _login(client, user)

        form = _form(name="Main List")
        del form["list_type"]
        client.post("/pricelists/create", data=form, follow_redirects=True)

        created = db_session.query(PriceList).filter_by(user_id=user.id).one()
        assert created.list_type == "permanent"

    def test_edit_can_switch_a_list_back_to_permanent(self, client, db_session):
        user = _create_user(db_session, "list_type_edit_user")
        _login(client, user)
        pricelist = PriceList(
            user_id=user.id,
            name="Was Limited",
            token="list-type-edit-token",
            is_active=True,
            list_type="limited",
        )
        db_session.add(pricelist)
        db_session.commit()

        client.post(
            f"/pricelists/{pricelist.id}/edit",
            data=_form(name="Was Limited", list_type="permanent"),
            follow_redirects=True,
        )

        db_session.refresh(pricelist)
        assert pricelist.list_type == "permanent"


class TestListIndexSections:
    def test_index_separates_permanent_and_limited_lists(self, client, db_session):
        user = _create_user(db_session, "list_type_index_user")
        _login(client, user)
        db_session.add_all(
            [
                PriceList(
                    user_id=user.id,
                    name="Main Catalogue",
                    token="list-type-main-token",
                    is_active=True,
                    list_type="permanent",
                ),
                PriceList(
                    user_id=user.id,
                    name="Weekend Drop",
                    token="list-type-limited-token",
                    is_active=True,
                    list_type="limited",
                    unpublish_at=utc_now() + timedelta(days=2, hours=1),
                ),
            ]
        )
        db_session.commit()

        html = client.get("/pricelists").get_data(as_text=True)

        assert "常設リスト（メイン）" in html
        assert "期間限定リスト" in html
        assert "Main Catalogue" in html
        assert "Weekend Drop" in html
        # The permanent section is rendered first, so its card must appear
        # before the limited heading.
        assert html.index("Main Catalogue") < html.index("⏳ 期間限定リスト")
        assert html.index("⏳ 期間限定リスト") < html.index("Weekend Drop")

    def test_index_shows_how_long_a_limited_list_has_left(self, client, db_session):
        user = _create_user(db_session, "list_type_countdown_user")
        _login(client, user)
        db_session.add(
            PriceList(
                user_id=user.id,
                name="Closing Soon",
                token="list-type-countdown-token",
                is_active=True,
                list_type="limited",
                unpublish_at=utc_now() + timedelta(days=2, hours=1),
            )
        )
        db_session.commit()

        html = client.get("/pricelists").get_data(as_text=True)

        assert "あと2日で自動的に非公開になります" in html

    def test_index_names_the_empty_section_rather_than_hiding_it(self, client, db_session):
        user = _create_user(db_session, "list_type_empty_user")
        _login(client, user)
        db_session.add(
            PriceList(
                user_id=user.id,
                name="Only Permanent",
                token="list-type-only-permanent-token",
                is_active=True,
                list_type="permanent",
            )
        )
        db_session.commit()

        html = client.get("/pricelists").get_data(as_text=True)

        assert "期間限定リストはまだありません。" in html
