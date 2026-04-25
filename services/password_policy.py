"""
Password policy shared by registration and account provisioning flows.
"""
from __future__ import annotations


MIN_PASSWORD_LENGTH = 12

COMMON_WEAK_PASSWORDS = {
    "password",
    "password1",
    "password12",
    "password123",
    "password1234",
    "admin",
    "admin123",
    "qwerty",
    "qwerty123",
    "letmein",
    "welcome",
    "welcome123",
    "test",
    "test123",
    "testpassword",
    "123456",
    "12345678",
    "123456789",
}


def validate_password_strength(password: str | None, username: str | None = None) -> list[str]:
    errors: list[str] = []
    candidate = password or ""
    normalized = candidate.strip().lower()
    normalized_user = (username or "").strip().lower()

    if not candidate:
        return ["Password is required."]

    if len(candidate) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    if normalized in COMMON_WEAK_PASSWORDS:
        errors.append("Password is too common.")

    if normalized_user and normalized_user in normalized:
        errors.append("Password must not contain the username.")

    has_letter = any(ch.isalpha() for ch in candidate)
    has_digit = any(ch.isdigit() for ch in candidate)
    if not (has_letter and has_digit):
        errors.append("Password must include both letters and numbers.")

    return errors
