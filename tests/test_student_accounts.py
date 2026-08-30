"""
The school office managing student accounts.

Suspension covers a break in attendance or an unpaid month as well as leaving,
so it is a switch that goes back: nothing is deleted, and resuming restores the
account and its published lists as they were. What matters here is that a
suspension actually stops things — a session already open, and the public
catalog — and that resuming loses nothing.
"""
import pytest
from flask import g

from models import PriceList, PriceListItem, Product, User
from services.student_account_service import (
    StudentAccountError,
    create_student,
    generate_temporary_password,
    normalize_email,
    normalize_username,
    remember_issued_password,
    reset_student_password,
    resume_student,
    suspend_student,
    take_issued_password,
)
from time_utils import utc_now


def _make_user(db_session, username, *, role="student", password="testpassword"):
    user = User(username=username, role=role, last_login_at=utc_now())
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username, password="testpassword"):
    return client.post(
        "/login", data={"username": username, "password": password}
    )


def _published_catalog(db_session, user, token="office-token"):
    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/{user.username}",
        last_title="商品",
        custom_title="商品",
        last_price=1000,
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()
    pricelist = PriceList(user_id=user.id, name="List", token=token, is_active=True)
    db_session.add(pricelist)
    db_session.commit()
    db_session.add(
        PriceListItem(price_list_id=pricelist.id, product_id=product.id, visible=True)
    )
    db_session.commit()
    return pricelist


class TestWhatTheOfficeMayType:
    @pytest.mark.parametrize("raw", ["student1", "  student1  ", "sato.hanako", "a@b.jp"])
    def test_a_workable_login_name_is_accepted(self, raw):
        assert normalize_username(raw) == raw.strip()

    @pytest.mark.parametrize("raw", ["", "   ", "ab", "name with spaces", "x" * 101, "<script>"])
    def test_anything_unusable_is_refused_with_a_reason(self, raw):
        with pytest.raises(StudentAccountError):
            normalize_username(raw)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("student@example.com", "student@example.com"),
            ("  student@example.com  ", "student@example.com"),
            ("", None),
            ("   ", None),
        ],
    )
    def test_an_address_is_tidied_and_an_empty_one_clears(self, raw, expected):
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize("raw", ["not-an-address", "a@b", "a b@example.com"])
    def test_an_address_that_cannot_work_is_refused(self, raw):
        with pytest.raises(StudentAccountError):
            normalize_email(raw)


class TestTemporaryPasswords:
    def test_two_are_never_the_same(self):
        assert len({generate_temporary_password() for _ in range(200)}) == 200

    def test_it_avoids_characters_that_are_misread_aloud(self):
        # These get read down a phone to a student who is not confident typing.
        joined = "".join(generate_temporary_password() for _ in range(50))
        for confusable in "l1IO0":
            assert confusable not in joined


class TestRegisteringAStudent:
    def test_the_password_comes_back_once_and_works(self, db_session, client):
        student, password = create_student(
            db_session, username="new_student", email="new@example.com"
        )

        assert student.email == "new@example.com"
        assert student.role == "student"
        assert not student.is_suspended
        # Returned, never stored in the clear.
        assert student.password_hash != password
        assert student.check_password(password)

        assert _login(client, "new_student", password).status_code == 302

    def test_a_name_already_taken_is_refused(self, db_session):
        _make_user(db_session, "taken_name")

        with pytest.raises(StudentAccountError) as failure:
            create_student(db_session, username="taken_name")

        assert "taken_name" in str(failure.value)


class TestSuspending:
    def test_a_suspended_student_cannot_sign_in(self, db_session, client):
        student = _make_user(db_session, "paused_student")
        suspend_student(db_session, student.id)

        response = _login(client, "paused_student")

        assert response.status_code == 403
        assert "利用を停止しています" in response.get_data(as_text=True)

    def test_a_session_already_open_stops_working(self, db_session, client):
        student = _make_user(db_session, "midsession_student")
        assert _login(client, "midsession_student").status_code == 302
        assert client.get("/").status_code == 200

        suspend_student(db_session, student.id)

        # The test fixture holds one app context open for the whole test, so
        # Flask-Login's per-context cache survives between these requests and
        # the loader never runs again. Production pushes a fresh context per
        # request; dropping the cached user is what that looks like from here.
        g.pop("_login_user", None)

        # Otherwise someone stopped mid-month keeps working until they log out.
        assert client.get("/").status_code == 302

    def test_the_public_catalog_goes_dark(self, client, db_session):
        student = _make_user(db_session, "catalog_student")
        _published_catalog(db_session, student, token="suspend-token")
        assert client.get("/catalog/suspend-token").status_code == 200

        suspend_student(db_session, student.id)

        assert client.get("/catalog/suspend-token").status_code == 404

    def test_nothing_is_taken_away(self, db_session):
        student = _make_user(db_session, "keeps_everything")
        pricelist = _published_catalog(db_session, student, token="keeps-token")

        suspend_student(db_session, student.id)
        db_session.expire_all()

        assert db_session.query(Product).filter_by(user_id=student.id).count() == 1
        assert db_session.get(PriceList, pricelist.id) is not None
        assert db_session.get(User, student.id) is not None

    def test_the_office_cannot_lock_itself_out(self, db_session):
        office = _make_user(db_session, "the_office", role="admin")

        with pytest.raises(StudentAccountError):
            suspend_student(db_session, office.id, acting_user_id=office.id)

    def test_no_office_action_can_target_an_admin(self, db_session):
        from services.student_account_service import (
            reset_student_password as _reset,
            resume_student as _resume,
            set_student_email as _set_email,
        )

        other_admin = _make_user(db_session, "another_admin", role="admin")
        original_hash = other_admin.password_hash

        # The id comes from the URL, so an admin must not be reachable through
        # routes that only claim to act on students.
        for action in (
            lambda: suspend_student(db_session, other_admin.id),
            lambda: _resume(db_session, other_admin.id),
            lambda: _reset(db_session, other_admin.id),
            lambda: _set_email(db_session, other_admin.id, "attacker@example.com"),
        ):
            with pytest.raises(StudentAccountError):
                action()

        db_session.expire_all()
        refreshed = db_session.get(User, other_admin.id)
        assert refreshed.password_hash == original_hash
        assert refreshed.email is None
        assert not refreshed.is_suspended

    def test_suspending_twice_keeps_the_first_time(self, db_session):
        student = _make_user(db_session, "twice_student")
        first = suspend_student(db_session, student.id).suspended_at

        again = suspend_student(db_session, student.id).suspended_at

        assert again == first


class TestResuming:
    def test_everything_comes_back(self, client, db_session):
        student = _make_user(db_session, "returning_student")
        _published_catalog(db_session, student, token="resume-token")
        suspend_student(db_session, student.id)
        assert client.get("/catalog/resume-token").status_code == 404

        resume_student(db_session, student.id)

        # The point of suspending rather than deleting: no rebuilding after a
        # break in attendance.
        assert client.get("/catalog/resume-token").status_code == 200
        assert _login(client, "returning_student").status_code == 302

    def test_resuming_an_active_account_changes_nothing(self, db_session):
        student = _make_user(db_session, "already_active")

        resume_student(db_session, student.id)

        assert not student.is_suspended


class TestResettingAPassword:
    def test_the_old_password_stops_working(self, db_session, client):
        student = _make_user(db_session, "forgetful_student", password="oldpassword")

        _student, issued = reset_student_password(db_session, student.id)

        assert _login(client, "forgetful_student", "oldpassword").status_code != 302
        assert _login(client, "forgetful_student", issued).status_code == 302


class TestHoldingAnIssuedPassword:
    def test_it_is_returned_once_and_then_gone(self):
        token = remember_issued_password("someone", "hunter2xyz", "reset")

        assert take_issued_password(token) == {
            "username": "someone",
            "password": "hunter2xyz",
            "reason": "reset",
        }
        # A second look, a shared screen, a back button: nothing there.
        assert take_issued_password(token) is None

    @pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
    def test_an_unknown_token_yields_nothing(self, token):
        assert take_issued_password(token) is None

    def test_it_does_not_survive_its_lifetime(self, monkeypatch):
        import services.student_account_service as service

        token = remember_issued_password("someone", "hunter2xyz", "created")
        clock = [service.time.monotonic() + service._ISSUED_PASSWORD_TTL_SECONDS + 1]
        monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])

        assert take_issued_password(token) is None

    def test_the_password_never_reaches_the_cookie(self, client, db_session):
        _make_user(db_session, "cookie_admin", role="admin")
        _login(client, "cookie_admin")

        # Before the dashboard loads and consumes it, so the cookie is caught
        # while it is actually carrying something.
        client.post(
            "/admin/students/create",
            data={"username": "cookie_student", "email": ""},
        )
        student = db_session.query(User).filter_by(username="cookie_student").one()

        with client.session_transaction() as flask_session:
            stored = str(flask_session.get("office_issued_password_token", ""))
        # Flask signs the session cookie but does not encrypt it, so whatever
        # is in there is readable by whoever holds it.
        assert stored
        assert not student.check_password(stored)


class TestOnlyTheOfficeCanDoThis:
    ADMIN_POSTS = [
        "/admin/students/create",
        "/admin/students/{id}/email",
        "/admin/students/{id}/reset-password",
        "/admin/students/{id}/suspend",
        "/admin/students/{id}/resume",
    ]

    @pytest.mark.parametrize("path", ADMIN_POSTS)
    def test_a_student_cannot_reach_them(self, client, db_session, path):
        victim = _make_user(db_session, "victim_student")
        _make_user(db_session, "ordinary_student")
        _login(client, "ordinary_student")

        response = client.post(path.format(id=victim.id), data={"email": "x@example.com"})

        # 404 rather than 403: the screen's existence is not advertised.
        assert response.status_code == 404
        db_session.expire_all()
        assert not db_session.get(User, victim.id).is_suspended

    @pytest.mark.parametrize("path", ADMIN_POSTS)
    def test_a_signed_out_visitor_cannot_reach_them(self, client, db_session, path):
        victim = _make_user(db_session, "logged_out_victim")

        response = client.post(path.format(id=victim.id), data={})

        assert response.status_code in {302, 401}
        db_session.expire_all()
        assert not db_session.get(User, victim.id).is_suspended


class TestTheOfficeScreen:
    def _office_client(self, client, db_session):
        _make_user(db_session, "office_admin", role="admin")
        _login(client, "office_admin")
        return client

    def test_a_new_password_is_shown_once_and_not_again(self, client, db_session):
        office = self._office_client(client, db_session)

        first = office.post(
            "/admin/students/create",
            data={"username": "shown_once", "email": ""},
            follow_redirects=True,
        ).get_data(as_text=True)
        student = db_session.query(User).filter_by(username="shown_once").one()

        assert "office-password-value" in first
        assert student.username in first

        # A reload must not still be showing it.
        assert "office-password-value" not in office.get("/admin").get_data(as_text=True)

    def test_suspending_and_resuming_from_the_screen(self, client, db_session):
        office = self._office_client(client, db_session)
        student = _make_user(db_session, "screen_student")

        office.post(f"/admin/students/{student.id}/suspend", follow_redirects=True)
        db_session.expire_all()
        assert db_session.get(User, student.id).is_suspended

        office.post(f"/admin/students/{student.id}/resume", follow_redirects=True)
        db_session.expire_all()
        assert not db_session.get(User, student.id).is_suspended

    def test_an_email_can_be_set_and_cleared(self, client, db_session):
        office = self._office_client(client, db_session)
        student = _make_user(db_session, "email_student")

        office.post(
            f"/admin/students/{student.id}/email",
            data={"email": "student@example.com"},
            follow_redirects=True,
        )
        db_session.expire_all()
        assert db_session.get(User, student.id).email == "student@example.com"

        office.post(
            f"/admin/students/{student.id}/email", data={"email": ""}, follow_redirects=True
        )
        db_session.expire_all()
        assert db_session.get(User, student.id).email is None

    def test_a_bad_address_is_explained_rather_than_crashing(self, client, db_session):
        office = self._office_client(client, db_session)
        student = _make_user(db_session, "bad_email_student")

        response = office.post(
            f"/admin/students/{student.id}/email",
            data={"email": "not-an-address"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert "メールアドレスの形式" in response.get_data(as_text=True)
        db_session.expire_all()
        assert db_session.get(User, student.id).email is None

    def test_a_suspended_student_is_not_listed_as_needing_a_nudge(self, client, db_session):
        from services.admin_dashboard_service import build_student_activity

        student = _make_user(db_session, "quiet_by_design")
        student.last_login_at = None
        db_session.commit()
        suspend_student(db_session, student.id)

        rows = build_student_activity(db_session)
        row = next(row for row in rows if row["username"] == "quiet_by_design")

        # It is quiet because the office made it quiet.
        assert row["is_suspended"] is True
        assert row["needs_follow_up"] is False
