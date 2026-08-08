"""
The school office's student-activity dashboard.

Two things matter most: students cannot reach it, and the follow-up rules pick
out the right people. The money figures are deliberately absent, and that is
asserted too — leaking a student's earnings to the office is the failure mode
this screen was designed around.
"""
from datetime import timedelta

from models import CatalogPageView, PriceList, Product, User
from services.admin_dashboard_service import (
    build_student_activity,
    summarize_student_activity,
)
from time_utils import utc_now


def _create_user(db_session, username, role="student", last_login_at=None):
    user = User(username=username, role=role, last_login_at=last_login_at)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username):
    return client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )


def _add_product(db_session, user, title="Item", status="active"):
    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/{user.id}/{title}",
        last_title=title,
        last_price=1000,
        status=status,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()
    return product


def _add_pricelist(db_session, user, token, list_type="permanent", is_active=True):
    pricelist = PriceList(
        user_id=user.id,
        name=f"List {token}",
        token=token,
        is_active=is_active,
        list_type=list_type,
    )
    db_session.add(pricelist)
    db_session.commit()
    return pricelist


def _add_view(db_session, pricelist, viewed_at):
    db_session.add(CatalogPageView(pricelist_id=pricelist.id, viewed_at=viewed_at))
    db_session.commit()


class TestAccessControl:
    def test_students_get_a_404_rather_than_a_hint_the_page_exists(self, client, db_session):
        _create_user(db_session, "plain_student", last_login_at=utc_now())
        _login(client, "plain_student")

        assert client.get("/admin").status_code == 404

    def test_signed_out_visitors_are_sent_to_login(self, client, db_session):
        response = client.get("/admin")

        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_admins_can_open_it(self, client, db_session):
        _create_user(db_session, "office_admin", role="admin", last_login_at=utc_now())
        _login(client, "office_admin")

        response = client.get("/admin")

        assert response.status_code == 200
        assert "生徒の利用状況" in response.get_data(as_text=True)

    def test_the_menu_entry_is_only_shown_to_admins(self, client, db_session):
        _create_user(db_session, "menu_student", last_login_at=utc_now())
        _login(client, "menu_student")
        assert "生徒の利用状況" not in client.get("/").get_data(as_text=True)

        client.get("/logout")
        _create_user(db_session, "menu_admin", role="admin", last_login_at=utc_now())
        _login(client, "menu_admin")
        assert "生徒の利用状況" in client.get("/").get_data(as_text=True)


class TestLoginTimestamp:
    def test_signing_in_records_the_time(self, client, db_session):
        user = _create_user(db_session, "login_stamp_user")
        assert user.last_login_at is None

        _login(client, "login_stamp_user")

        db_session.refresh(user)
        assert user.last_login_at is not None

    def test_a_failed_sign_in_does_not_record_a_time(self, client, db_session):
        user = _create_user(db_session, "bad_password_user")

        client.post(
            "/login",
            data={"username": "bad_password_user", "password": "wrong-password"},
        )

        db_session.refresh(user)
        assert user.last_login_at is None


class TestFollowUpRules:
    def test_a_student_who_has_not_logged_in_for_two_weeks_is_flagged(self, db_session):
        now = utc_now()
        _create_user(db_session, "quiet_user", last_login_at=now - timedelta(days=15))

        row = build_student_activity(db_session, now)[0]

        assert row["needs_follow_up"] is True
        assert "14日以上ログインがありません" in row["follow_up_reasons"]

    def test_a_student_who_logged_in_yesterday_is_not_flagged(self, db_session):
        now = utc_now()
        _create_user(db_session, "recent_user", last_login_at=now - timedelta(days=1))

        row = build_student_activity(db_session, now)[0]

        assert row["needs_follow_up"] is False

    def test_a_student_who_never_logged_in_is_flagged(self, db_session):
        now = utc_now()
        _create_user(db_session, "never_user")

        row = build_student_activity(db_session, now)[0]

        assert "14日以上ログインがありません" in row["follow_up_reasons"]

    def test_products_without_a_price_list_are_flagged(self, db_session):
        now = utc_now()
        user = _create_user(db_session, "no_list_user", last_login_at=now)
        _add_product(db_session, user)

        row = build_student_activity(db_session, now)[0]

        assert "商品はあるのに価格表がまだありません" in row["follow_up_reasons"]

    def test_no_products_and_no_price_list_is_not_flagged_for_that_reason(self, db_session):
        now = utc_now()
        _create_user(db_session, "brand_new_user", last_login_at=now)

        row = build_student_activity(db_session, now)[0]

        assert "商品はあるのに価格表がまだありません" not in row["follow_up_reasons"]

    def test_a_published_list_nobody_looked_at_is_flagged(self, db_session):
        now = utc_now()
        user = _create_user(db_session, "unseen_user", last_login_at=now)
        pricelist = _add_pricelist(db_session, user, "admin-unseen-token")
        _add_view(db_session, pricelist, now - timedelta(days=30))

        row = build_student_activity(db_session, now)[0]

        assert "公開中の価格表が直近7日で見られていません" in row["follow_up_reasons"]

    def test_a_list_viewed_this_week_is_not_flagged(self, db_session):
        now = utc_now()
        user = _create_user(db_session, "seen_user", last_login_at=now)
        pricelist = _add_pricelist(db_session, user, "admin-seen-token")
        _add_view(db_session, pricelist, now - timedelta(days=2))

        row = build_student_activity(db_session, now)[0]

        assert row["needs_follow_up"] is False

    def test_views_halving_month_on_month_is_flagged(self, db_session):
        now = utc_now()
        user = _create_user(db_session, "dropping_user", last_login_at=now)
        pricelist = _add_pricelist(db_session, user, "admin-drop-token")
        for _ in range(10):
            _add_view(db_session, pricelist, now - timedelta(days=45))
        _add_view(db_session, pricelist, now - timedelta(days=2))

        row = build_student_activity(db_session, now)[0]

        assert "閲覧数が先月の半分以下に減っています" in row["follow_up_reasons"]

    def test_steady_views_are_not_flagged_as_a_drop(self, db_session):
        now = utc_now()
        user = _create_user(db_session, "steady_user", last_login_at=now)
        pricelist = _add_pricelist(db_session, user, "admin-steady-token")
        for _ in range(5):
            _add_view(db_session, pricelist, now - timedelta(days=45))
            _add_view(db_session, pricelist, now - timedelta(days=2))

        row = build_student_activity(db_session, now)[0]

        assert "閲覧数が先月の半分以下に減っています" not in row["follow_up_reasons"]


class TestCounts:
    def test_products_and_lists_are_counted_by_kind(self, db_session):
        now = utc_now()
        user = _create_user(db_session, "counting_user", last_login_at=now)
        _add_product(db_session, user, "Published", status="active")
        _add_product(db_session, user, "Draft one", status="draft")
        _add_product(db_session, user, "Draft two", status="draft")
        _add_pricelist(db_session, user, "admin-count-permanent")
        _add_pricelist(db_session, user, "admin-count-limited", list_type="limited")

        row = build_student_activity(db_session, now)[0]

        assert row["active_product_count"] == 1
        assert row["draft_product_count"] == 2
        assert row["product_count"] == 3
        assert row["permanent_list_count"] == 1
        assert row["limited_list_count"] == 1

    def test_deleted_products_are_not_counted(self, db_session):
        now = utc_now()
        user = _create_user(db_session, "trash_user", last_login_at=now)
        product = _add_product(db_session, user, "Trashed")
        product.deleted_at = now
        db_session.commit()

        row = build_student_activity(db_session, now)[0]

        assert row["product_count"] == 0

    def test_admins_are_left_out_of_the_student_list(self, db_session):
        now = utc_now()
        _create_user(db_session, "an_admin", role="admin", last_login_at=now)
        _create_user(db_session, "a_student", last_login_at=now)

        usernames = [row["username"] for row in build_student_activity(db_session, now)]

        assert usernames == ["a_student"]

    def test_one_students_activity_is_never_attributed_to_another(self, db_session):
        now = utc_now()
        first = _create_user(db_session, "aaa_student", last_login_at=now)
        second = _create_user(db_session, "bbb_student", last_login_at=now)
        _add_product(db_session, first, "Only first")
        pricelist = _add_pricelist(db_session, second, "admin-isolation-token")
        _add_view(db_session, pricelist, now - timedelta(days=1))

        rows = {row["username"]: row for row in build_student_activity(db_session, now)}

        assert rows["aaa_student"]["product_count"] == 1
        assert rows["aaa_student"]["total_view_count"] == 0
        assert rows["bbb_student"]["product_count"] == 0
        assert rows["bbb_student"]["total_view_count"] == 1

    def test_summary_totals_the_rows(self, db_session):
        now = utc_now()
        first = _create_user(db_session, "sum_one", last_login_at=now)
        _create_user(db_session, "sum_two", last_login_at=now - timedelta(days=20))
        _add_product(db_session, first, "Counted")
        pricelist = _add_pricelist(db_session, first, "admin-summary-token")
        # Viewed this week, so only the student who stopped logging in is flagged.
        _add_view(db_session, pricelist, now - timedelta(days=1))

        summary = summarize_student_activity(build_student_activity(db_session, now))

        assert summary["student_count"] == 2
        assert summary["product_count"] == 1
        assert summary["pricelist_count"] == 1
        assert summary["started_count"] == 1
        assert summary["follow_up_count"] == 1


class TestWhatIsNotShown:
    def test_the_page_shows_no_sales_or_cost_figures(self, client, db_session):
        now = utc_now()
        _create_user(db_session, "privacy_admin", role="admin", last_login_at=now)
        student = _create_user(db_session, "privacy_student", last_login_at=now)
        product = _add_product(db_session, student, "Valuable")
        product.selling_price = 987654
        product.last_price = 123456
        db_session.commit()

        _login(client, "privacy_admin")
        html = client.get("/admin").get_data(as_text=True)

        assert "987,654" not in html
        assert "987654" not in html
        assert "123,456" not in html
        assert "123456" not in html
        assert "利益" not in html


class TestNavigationState:
    def test_the_student_dashboard_link_is_not_marked_active_on_the_admin_screen(
        self, client, db_session
    ):
        _create_user(db_session, "nav_admin", role="admin", last_login_at=utc_now())
        _login(client, "nav_admin")

        html = client.get("/admin").get_data(as_text=True)

        # Both endpoints end in "dashboard"; only the admin entry should be lit.
        assert 'class="sidebar-link active"' not in html.split("生徒の利用状況")[0]
