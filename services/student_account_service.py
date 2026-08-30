"""
Student accounts, as the school office manages them.

The office registers students, so it is the office that holds their email
address and that starts and stops their access. Nothing here deletes an
account: a break in attendance or an unpaid month is a switch that goes back,
and a deletion that took a student's products with it would be an accident
waiting to happen.
"""
from __future__ import annotations

import re
import secrets
import string

from models import User
from time_utils import utc_now


class StudentAccountError(ValueError):
    """Something the office typed cannot be used, with a reason to show them."""


#: Long enough to be safe to say aloud once, short enough to type. Ambiguous
#: characters are left out because these get read over the phone.
_TEMPORARY_PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789"
_TEMPORARY_PASSWORD_LENGTH = 10

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._@+-]{3,100}$")
# Deliberately permissive: the office types these from an application form, and
# a rejected address they can see is worse than an unusual one they meant.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


def generate_temporary_password() -> str:
    return "".join(
        secrets.choice(_TEMPORARY_PASSWORD_ALPHABET)
        for _ in range(_TEMPORARY_PASSWORD_LENGTH)
    )


def normalize_username(raw_value) -> str:
    value = str(raw_value or "").strip()
    if not value:
        raise StudentAccountError("ログイン名を入力してください。")
    if not _USERNAME_PATTERN.match(value):
        raise StudentAccountError(
            "ログイン名は3〜100文字で、英数字と . _ @ + - のみ使用できます。"
        )
    return value


def normalize_email(raw_value) -> str | None:
    """Return a stored address, or None for a cleared one."""
    value = str(raw_value or "").strip()
    if not value:
        return None
    if len(value) > 255:
        raise StudentAccountError("メールアドレスが長すぎます。")
    if not _EMAIL_PATTERN.match(value):
        raise StudentAccountError("メールアドレスの形式を確認してください。")
    return value


def create_student(session_db, *, username, email=None) -> tuple[User, str]:
    """
    Register a student and return them with the password to hand over.

    The password is generated rather than chosen, so it exists only in this
    return value and in the hash — the office reads it out once and the student
    changes it.
    """
    username = normalize_username(username)
    email = normalize_email(email)

    if session_db.query(User).filter_by(username=username).first() is not None:
        raise StudentAccountError(f"ログイン名「{username}」はすでに使われています。")

    password = generate_temporary_password()
    student = User(username=username, email=email, role="student")
    student.set_password(password)
    session_db.add(student)
    session_db.commit()
    return student, password


def _student_for_office(session_db, user_id) -> User:
    student = session_db.get(User, user_id)
    if student is None:
        raise StudentAccountError("その生徒が見つかりませんでした。")
    return student


def set_student_email(session_db, user_id, raw_email) -> User:
    student = _student_for_office(session_db, user_id)
    student.email = normalize_email(raw_email)
    session_db.commit()
    return student


def reset_student_password(session_db, user_id) -> tuple[User, str]:
    """Issue a new temporary password and return it once."""
    student = _student_for_office(session_db, user_id)
    password = generate_temporary_password()
    student.set_password(password)
    session_db.commit()
    return student, password


def suspend_student(session_db, user_id, *, acting_user_id=None) -> User:
    """
    Stop an account without taking anything away.

    Their sessions stop working, their published lists stop answering, and
    every product, price list and setting stays exactly where it was.
    """
    student = _student_for_office(session_db, user_id)
    if acting_user_id is not None and student.id == acting_user_id:
        # Locking yourself out of the screen that unlocks accounts.
        raise StudentAccountError("自分自身のアカウントは停止できません。")
    if student.is_admin:
        raise StudentAccountError("管理者アカウントは停止できません。")
    if not student.is_suspended:
        student.suspended_at = utc_now()
        session_db.commit()
    return student


def resume_student(session_db, user_id) -> User:
    """Put the account back exactly as it was."""
    student = _student_for_office(session_db, user_id)
    if student.is_suspended:
        student.suspended_at = None
        session_db.commit()
    return student
