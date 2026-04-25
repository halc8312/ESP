"""
Application security configuration helpers.

The app is still import-driven rather than factory-based, so these helpers keep
environment parsing and production fail-closed checks isolated and testable.
"""
from __future__ import annotations

import os
from typing import Mapping, MutableMapping


DEFAULT_DEV_SECRET = "dev-secret-key-change-this"
PRODUCTION_ROLES = {"web", "worker"}
PRODUCTION_ENV_NAMES = {"production", "prod"}
DEFAULT_HSTS_SECONDS = 31536000


class SecurityConfigurationError(RuntimeError):
    """Raised when production security configuration is unsafe."""


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_production_runtime(environ: Mapping[str, str] | None = None) -> bool:
    env = environ or os.environ
    runtime_role = (env.get("RUNTIME_ROLE") or env.get("runtime_role") or "").strip().lower()
    app_env = (
        env.get("APP_ENV")
        or env.get("FLASK_ENV")
        or env.get("ENVIRONMENT")
        or env.get("PYTHON_ENV")
        or ""
    ).strip().lower()

    return (
        runtime_role in PRODUCTION_ROLES
        or app_env in PRODUCTION_ENV_NAMES
        or parse_bool(env.get("RENDER"), default=False)
    )


def _safe_samesite(value: str | None) -> str:
    normalized = (value or "Lax").strip().capitalize()
    return normalized if normalized in {"Lax", "Strict", "None"} else "Lax"


def validate_secret_key(environ: Mapping[str, str], production: bool) -> str:
    secret_key = (environ.get("SECRET_KEY") or "").strip()

    if production:
        if not secret_key:
            raise SecurityConfigurationError("SECRET_KEY must be set in production.")
        if secret_key == DEFAULT_DEV_SECRET:
            raise SecurityConfigurationError("SECRET_KEY must not use the development default in production.")
        if len(secret_key) < 32:
            raise SecurityConfigurationError("SECRET_KEY must be at least 32 characters in production.")

    return secret_key or DEFAULT_DEV_SECRET


def validate_rate_limit_store(environ: Mapping[str, str], production: bool) -> None:
    if production and not (environ.get("REDIS_URL") or environ.get("VALKEY_URL")):
        raise SecurityConfigurationError("REDIS_URL or VALKEY_URL must be set for production rate limiting.")


def configure_app_security(app, environ: MutableMapping[str, str] | None = None) -> None:
    env = environ or os.environ
    production = is_production_runtime(env)

    app.secret_key = validate_secret_key(env, production)
    validate_rate_limit_store(env, production)

    app.config["APP_ENV"] = env.get("APP_ENV") or ("production" if production else "development")
    app.config["IS_PRODUCTION"] = production
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = _safe_samesite(env.get("SESSION_COOKIE_SAMESITE"))
    session_cookie_secure = parse_bool(env.get("SESSION_COOKIE_SECURE"), default=production)
    force_https = parse_bool(env.get("FORCE_HTTPS"), default=production)
    hsts_enabled = parse_bool(env.get("HSTS_ENABLED"), default=production)
    if production:
        session_cookie_secure = True
        force_https = True
        hsts_enabled = True

    app.config["SESSION_COOKIE_SECURE"] = session_cookie_secure
    app.config["PREFERRED_URL_SCHEME"] = "https" if production else "http"
    app.config["FORCE_HTTPS"] = force_https
    app.config["HSTS_ENABLED"] = hsts_enabled
    app.config["HSTS_MAX_AGE"] = int(env.get("HSTS_MAX_AGE", DEFAULT_HSTS_SECONDS))
    app.config["HSTS_INCLUDE_SUBDOMAINS"] = parse_bool(env.get("HSTS_INCLUDE_SUBDOMAINS"), default=True)
    app.config["HSTS_PRELOAD"] = parse_bool(env.get("HSTS_PRELOAD"), default=False)
    app.config["MAX_CONTENT_LENGTH"] = int(env.get("MAX_CONTENT_LENGTH", str(8 * 1024 * 1024)))

    app.config["ALLOW_PUBLIC_SIGNUP"] = parse_bool(
        env.get("ALLOW_PUBLIC_SIGNUP"),
        default=not production,
    )
    app.config["LOGIN_RATE_LIMIT"] = int(env.get("LOGIN_RATE_LIMIT", "5"))
    app.config["LOGIN_RATE_WINDOW_SECONDS"] = int(env.get("LOGIN_RATE_WINDOW_SECONDS", "900"))
    app.config["REGISTER_RATE_LIMIT"] = int(env.get("REGISTER_RATE_LIMIT", "3"))
    app.config["REGISTER_RATE_WINDOW_SECONDS"] = int(env.get("REGISTER_RATE_WINDOW_SECONDS", "3600"))


def build_hsts_header(app) -> str:
    parts = [f"max-age={int(app.config.get('HSTS_MAX_AGE', DEFAULT_HSTS_SECONDS))}"]
    if app.config.get("HSTS_INCLUDE_SUBDOMAINS", True):
        parts.append("includeSubDomains")
    if app.config.get("HSTS_PRELOAD"):
        parts.append("preload")
    return "; ".join(parts)
