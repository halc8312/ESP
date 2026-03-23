"""
Flask application assembly and runtime bootstrap helpers.
"""
import os
import tempfile
from typing import Any

from flask import Flask, send_from_directory
from flask_apscheduler import APScheduler
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

from database import SessionLocal, bootstrap_schema
from models import User
from services.image_service import IMAGE_STORAGE_PATH

try:
    import fcntl as _fcntl
except ModuleNotFoundError:
    _fcntl = None


login_manager = LoginManager()


class SchedulerConfig:
    SCHEDULER_API_ENABLED = True


RUNTIME_DEFAULTS: dict[str, dict[str, Any]] = {
    "base": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        "SCHEMA_BOOTSTRAP_MODE": "disabled",
        "ENABLE_LEGACY_SCHEMA_PATCHSET": False,
        "ENABLE_SCHEDULER": False,
    },
    "web": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": True,
        "SCHEMA_BOOTSTRAP_MODE": os.environ.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
        "ENABLE_LEGACY_SCHEMA_PATCHSET": True,
        "ENABLE_SCHEDULER": True,
    },
    "cli": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": True,
        "SCHEMA_BOOTSTRAP_MODE": os.environ.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
        "ENABLE_LEGACY_SCHEMA_PATCHSET": True,
        "ENABLE_SCHEDULER": False,
    },
    "worker": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        "SCHEMA_BOOTSTRAP_MODE": "disabled",
        "ENABLE_LEGACY_SCHEMA_PATCHSET": False,
        "ENABLE_SCHEDULER": False,
    },
    "test": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        "SCHEMA_BOOTSTRAP_MODE": "disabled",
        "ENABLE_LEGACY_SCHEMA_PATCHSET": False,
        "ENABLE_SCHEDULER": False,
    },
}

_SCHEDULER_LOCK_PATH = os.path.join(tempfile.gettempdir(), "esp_scheduler.lock")


@login_manager.user_loader
def load_user(user_id):
    session_db = SessionLocal()
    try:
        return session_db.query(User).get(int(user_id))
    finally:
        session_db.close()


def _get_runtime_defaults(runtime_role: str) -> dict[str, Any]:
    return dict(RUNTIME_DEFAULTS.get(runtime_role, RUNTIME_DEFAULTS["base"]))


def _register_blueprints(app: Flask) -> None:
    from routes.api import api_bp
    from routes.archive import archive_bp
    from routes.auth import auth_bp
    from routes.catalog import catalog_bp
    from routes.export import export_bp
    from routes.import_routes import import_bp
    from routes.main import main_bp
    from routes.pricelist import pricelist_bp
    from routes.pricing import pricing_bp
    from routes.products import products_bp
    from routes.scrape import scrape_bp
    from routes.settings import settings_bp
    from routes.shops import shops_bp
    from routes.trash import trash_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(shops_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(scrape_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(pricing_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(import_bp)
    app.register_blueprint(archive_bp)
    app.register_blueprint(trash_bp)
    app.register_blueprint(pricelist_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(api_bp)


def _register_backward_compat_aliases(app: Flask) -> None:
    app.add_url_rule("/", endpoint="index", view_func=lambda: None, build_only=True)
    app.add_url_rule("/dashboard", endpoint="dashboard", view_func=lambda: None, build_only=True)
    app.add_url_rule("/login", endpoint="login", view_func=lambda: None, build_only=True)
    app.add_url_rule("/register", endpoint="register", view_func=lambda: None, build_only=True)
    app.add_url_rule("/logout", endpoint="logout", view_func=lambda: None, build_only=True)
    app.add_url_rule("/shops", endpoint="manage_shops", view_func=lambda: None, build_only=True)
    app.add_url_rule("/templates", endpoint="manage_templates", view_func=lambda: None, build_only=True)
    app.add_url_rule("/set_current_shop", endpoint="set_current_shop", view_func=lambda: None, build_only=True)
    app.add_url_rule(
        "/product/<int:product_id>",
        endpoint="product_detail",
        view_func=lambda product_id: None,
        build_only=True,
    )
    app.add_url_rule(
        "/shops/<int:shop_id>/delete",
        endpoint="delete_shop",
        view_func=lambda shop_id: None,
        build_only=True,
    )
    app.add_url_rule(
        "/templates/<int:template_id>/delete",
        endpoint="delete_template",
        view_func=lambda template_id: None,
        build_only=True,
    )
    app.add_url_rule("/scrape", endpoint="scrape_form", view_func=lambda: None, build_only=True)
    app.add_url_rule("/scrape/run", endpoint="scrape_run", view_func=lambda: None, build_only=True)
    app.add_url_rule("/export/shopify", endpoint="export_shopify", view_func=lambda: None, build_only=True)
    app.add_url_rule("/export_ebay", endpoint="export_ebay", view_func=lambda: None, build_only=True)
    app.add_url_rule(
        "/export_stock_update",
        endpoint="export_stock_update",
        view_func=lambda: None,
        build_only=True,
    )
    app.add_url_rule(
        "/export_price_update",
        endpoint="export_price_update",
        view_func=lambda: None,
        build_only=True,
    )


def _register_media_route(app: Flask) -> None:
    @app.route("/media/<path:filename>")
    def serve_image(filename):
        return send_from_directory(IMAGE_STORAGE_PATH, filename)


def _register_cli_commands(app: Flask) -> None:
    from cli import register_cli_commands as register_app_cli_commands
    from routes.auth import register_cli_commands as register_auth_cli_commands

    register_auth_cli_commands(app)
    register_app_cli_commands(app)


def _create_scheduler(app: Flask) -> None:
    scheduler = APScheduler()
    scheduler.init_app(app)
    app.extensions["esp_scheduler"] = scheduler


def create_app(runtime_role: str = "base", config_overrides: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(SchedulerConfig())
    app.config.update(_get_runtime_defaults(runtime_role))
    app.config.update(
        {
            "ESP_RUNTIME_ROLE": runtime_role,
            "SECRET_KEY": os.environ.get("SECRET_KEY", "dev-secret-key-change-this"),
            "SCRAPE_QUEUE_BACKEND": os.environ.get("SCRAPE_QUEUE_BACKEND", "inmemory"),
        }
    )
    if config_overrides:
        app.config.update(config_overrides)

    app.secret_key = app.config["SECRET_KEY"]
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    _create_scheduler(app)
    _register_blueprints(app)
    _register_backward_compat_aliases(app)
    _register_media_route(app)
    _register_cli_commands(app)

    return app


def run_legacy_startup_migrations() -> None:
    """Safely add missing columns to existing tables."""
    from sqlalchemy import text

    from database import engine

    migrations = [
        ("products", "pricing_rule_id", "ALTER TABLE products ADD COLUMN pricing_rule_id INTEGER"),
        ("products", "selling_price", "ALTER TABLE products ADD COLUMN selling_price INTEGER"),
        ("products", "custom_title_en", "ALTER TABLE products ADD COLUMN custom_title_en VARCHAR"),
        ("products", "custom_description_en", "ALTER TABLE products ADD COLUMN custom_description_en TEXT"),
        ("products", "archived", "ALTER TABLE products ADD COLUMN archived BOOLEAN DEFAULT FALSE"),
        ("products", "deleted_at", "ALTER TABLE products ADD COLUMN deleted_at DATETIME"),
        ("price_lists", "layout", "ALTER TABLE price_lists ADD COLUMN layout VARCHAR DEFAULT 'grid'"),
        ("shops", "logo_url", "ALTER TABLE shops ADD COLUMN logo_url VARCHAR"),
        ("products", "patrol_fail_count", "ALTER TABLE products ADD COLUMN patrol_fail_count INTEGER DEFAULT 0"),
    ]

    with engine.connect() as conn:
        for table, column, sql in migrations:
            try:
                conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                    print(f"Migration: Added column {column} to {table}")
                except Exception as exc:
                    print(f"Migration error for {column}: {exc}")


def _register_scheduler_jobs(app: Flask) -> None:
    if app.extensions.get("esp_scheduler_jobs_registered"):
        return

    scheduler: APScheduler = app.extensions["esp_scheduler"]

    def patrol_job():
        from services.monitor_service import MonitorService

        with app.app_context():
            MonitorService.check_stale_products(limit=15)

    def trash_purge_job():
        import logging

        from routes.trash import purge_old_trash

        with app.app_context():
            logger = logging.getLogger("trash")
            count = purge_old_trash()
            if count > 0:
                logger.info("Auto-purged %s items from trash", count)

    scheduler.add_job(
        id="patrol_job",
        func=patrol_job,
        trigger="interval",
        minutes=15,
        replace_existing=True,
    )
    scheduler.add_job(
        id="trash_purge_job",
        func=trash_purge_job,
        trigger="cron",
        hour=3,
        replace_existing=True,
    )
    app.extensions["esp_scheduler_jobs_registered"] = True


def _acquire_scheduler_lock():
    if _fcntl is None:
        return True, None

    lock_fd = open(_SCHEDULER_LOCK_PATH, "w")
    try:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except (IOError, OSError):
        lock_fd.close()
        return False, None

    return True, lock_fd


def start_scheduler(app: Flask) -> bool:
    scheduler: APScheduler = app.extensions["esp_scheduler"]
    if app.extensions.get("esp_scheduler_started"):
        return True

    acquired, lock_fd = _acquire_scheduler_lock()
    if not acquired:
        return False

    _register_scheduler_jobs(app)
    scheduler.start()
    app.extensions["esp_scheduler_started"] = True
    app.extensions["esp_scheduler_lock_fd"] = lock_fd
    return True


def initialize_app_runtime(app: Flask) -> Flask:
    if app.extensions.get("esp_runtime_initialized"):
        return app

    if app.config.get("RUN_SCHEMA_BOOTSTRAP_ON_STARTUP"):
        with app.app_context():
            applied_schema_mode = bootstrap_schema(app.config.get("SCHEMA_BOOTSTRAP_MODE", "auto"))
            app.extensions["esp_schema_bootstrap_mode"] = applied_schema_mode
            if applied_schema_mode == "legacy" and app.config.get("ENABLE_LEGACY_SCHEMA_PATCHSET"):
                run_legacy_startup_migrations()

    if app.config.get("ENABLE_SCHEDULER"):
        start_scheduler(app)

    app.extensions["esp_runtime_initialized"] = True
    return app


def create_runtime_app(
    runtime_role: str,
    config_overrides: dict[str, Any] | None = None,
) -> Flask:
    return initialize_app_runtime(create_app(runtime_role=runtime_role, config_overrides=config_overrides))


def create_web_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    return create_runtime_app("web", config_overrides=config_overrides)


def create_cli_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    return create_runtime_app("cli", config_overrides=config_overrides)


def create_worker_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    return create_runtime_app("worker", config_overrides=config_overrides)


app = create_app(runtime_role="base")


if __name__ == "__main__":
    runtime_app = create_web_app()
    runtime_app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
