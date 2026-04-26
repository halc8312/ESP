import pytest
from flask import Flask

from security_config import SecurityConfigurationError, configure_app_security


def test_production_requires_secret_key():
    app = Flask(__name__)

    with pytest.raises(SecurityConfigurationError, match="SECRET_KEY"):
        configure_app_security(app, {"APP_ENV": "production", "REDIS_URL": "redis://localhost:6379/0"})


def test_production_rejects_default_secret_key():
    app = Flask(__name__)

    with pytest.raises(SecurityConfigurationError, match="development default"):
        configure_app_security(
            app,
            {
                "APP_ENV": "production",
                "SECRET_KEY": "dev-secret-key-change-this",
                "REDIS_URL": "redis://localhost:6379/0",
            },
        )


def test_production_requires_shared_rate_limit_store():
    app = Flask(__name__)

    with pytest.raises(SecurityConfigurationError, match="REDIS_URL"):
        configure_app_security(
            app,
            {
                "APP_ENV": "production",
                "SECRET_KEY": "x" * 32,
            },
        )


def test_production_enforces_cookie_https_and_hsts_config():
    app = Flask(__name__)

    configure_app_security(
        app,
        {
            "APP_ENV": "production",
            "SECRET_KEY": "x" * 32,
            "REDIS_URL": "redis://localhost:6379/0",
        },
    )

    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["FORCE_HTTPS"] is True
    assert app.config["HSTS_ENABLED"] is True
    assert app.config["ALLOW_PUBLIC_SIGNUP"] is False


def test_production_does_not_allow_disabling_https_controls():
    app = Flask(__name__)

    configure_app_security(
        app,
        {
            "APP_ENV": "production",
            "SECRET_KEY": "x" * 32,
            "REDIS_URL": "redis://localhost:6379/0",
            "SESSION_COOKIE_SECURE": "false",
            "FORCE_HTTPS": "false",
            "HSTS_ENABLED": "false",
        },
    )

    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["FORCE_HTTPS"] is True
    assert app.config["HSTS_ENABLED"] is True


def test_development_allows_default_secret_and_local_rate_limit():
    app = Flask(__name__)

    configure_app_security(app, {"APP_ENV": "development"})

    assert app.secret_key == "dev-secret-key-change-this"
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["ALLOW_PUBLIC_SIGNUP"] is True
