"""
Flask application assembly and runtime bootstrap helpers.
"""
import os
import tempfile
import threading
from typing import Any

import logging

from flask import Flask, render_template, send_from_directory
from flask_wtf.csrf import CSRFProtect
from flask_apscheduler import APScheduler
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

from database import SessionLocal, bootstrap_schema, ensure_additive_schema_ready
from models import User
from services.image_service import IMAGE_STORAGE_PATH

try:
    import fcntl as _fcntl
except ModuleNotFoundError:
    _fcntl = None


login_manager = LoginManager()
csrf = CSRFProtect()


class SchedulerConfig:
    SCHEDULER_API_ENABLED = True


RUNTIME_DEFAULTS: dict[str, dict[str, Any]] = {
    "base": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        "SCHEMA_BOOTSTRAP_MODE": "disabled",
        "ENABLE_LEGACY_SCHEMA_PATCHSET": False,
        "ENABLE_SCHEDULER": False,
        "REGISTER_BLUEPRINTS": True,
        "REGISTER_BACKWARD_COMPAT_ALIASES": True,
        "REGISTER_MEDIA_ROUTE": True,
        "REGISTER_CLI_COMMANDS": True,
    },
    "web": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": True,
        "SCHEMA_BOOTSTRAP_MODE": os.environ.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
        "ENABLE_LEGACY_SCHEMA_PATCHSET": True,
        "VERIFY_SCHEMA_DRIFT_ON_STARTUP": True,
        "ENABLE_SCHEDULER": False,
        "WEB_SCHEDULER_MODE": os.environ.get("WEB_SCHEDULER_MODE", "auto"),
        "REGISTER_BLUEPRINTS": True,
        "REGISTER_BACKWARD_COMPAT_ALIASES": True,
        "REGISTER_MEDIA_ROUTE": True,
        "REGISTER_CLI_COMMANDS": True,
    },
    "cli": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": True,
        "SCHEMA_BOOTSTRAP_MODE": os.environ.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
        "ENABLE_LEGACY_SCHEMA_PATCHSET": True,
        "VERIFY_SCHEMA_DRIFT_ON_STARTUP": True,
        "ENABLE_SCHEDULER": False,
        "REGISTER_BLUEPRINTS": True,
        "REGISTER_BACKWARD_COMPAT_ALIASES": True,
        "REGISTER_MEDIA_ROUTE": True,
        "REGISTER_CLI_COMMANDS": True,
    },
    "worker": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        "SCHEMA_BOOTSTRAP_MODE": "disabled",
        "ENABLE_LEGACY_SCHEMA_PATCHSET": False,
        "ENABLE_SCHEDULER": False,
        "REGISTER_BLUEPRINTS": False,
        "REGISTER_BACKWARD_COMPAT_ALIASES": False,
        "REGISTER_MEDIA_ROUTE": False,
        "REGISTER_CLI_COMMANDS": False,
    },
    "test": {
        "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        "SCHEMA_BOOTSTRAP_MODE": "disabled",
        "ENABLE_LEGACY_SCHEMA_PATCHSET": False,
        "ENABLE_SCHEDULER": False,
        "REGISTER_BLUEPRINTS": True,
        "REGISTER_BACKWARD_COMPAT_ALIASES": True,
        "REGISTER_MEDIA_ROUTE": True,
        "REGISTER_CLI_COMMANDS": True,
    },
}

_SCHEDULER_LOCK_PATH = os.path.join(tempfile.gettempdir(), "esp_scheduler.lock")


@login_manager.user_loader
def load_user(user_id):
    session_db = SessionLocal()
    try:
        return session_db.query(User).get(int(user_id))
    except Exception:
        session_db.rollback()
        raise
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


def _register_health_route(app: Flask) -> None:
    @app.route("/healthz")
    def healthz():
        return {
            "status": "ok",
            "runtime_role": app.config.get("ESP_RUNTIME_ROLE", "base"),
            "queue_backend": str(app.config.get("SCRAPE_QUEUE_BACKEND", "inmemory") or "inmemory").strip().lower(),
            "scheduler_enabled": bool(app.config.get("ENABLE_SCHEDULER", False)),
        }


def _register_cli_commands(app: Flask) -> None:
    from cli import register_cli_commands as register_app_cli_commands
    from routes.auth import register_cli_commands as register_auth_cli_commands

    register_auth_cli_commands(app)
    register_app_cli_commands(app)


def _create_scheduler(app: Flask) -> None:
    scheduler = APScheduler()
    scheduler.init_app(app)
    app.extensions["esp_scheduler"] = scheduler


def _resolve_web_scheduler_enabled(app: Flask) -> bool:
    mode = str(app.config.get("WEB_SCHEDULER_MODE", "auto") or "auto").strip().lower()
    if mode == "enabled":
        return True
    if mode == "disabled":
        return False
    if mode != "auto":
        raise ValueError(f"Unsupported WEB_SCHEDULER_MODE: {mode}")

    queue_backend = str(app.config.get("SCRAPE_QUEUE_BACKEND", "inmemory") or "inmemory").strip().lower()
    return queue_backend == "inmemory"


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="ページが見つかりません"), 404

    @app.errorhandler(500)
    def internal_error(_error):
        return render_template("error.html", code=500, message="サーバー内部エラーが発生しました"), 500


def create_app(runtime_role: str = "base", config_overrides: dict[str, Any] | None = None) -> Flask:
    logger = logging.getLogger("esp.app")
    app = Flask(__name__)
    app.config.from_object(SchedulerConfig())
    app.config.update(_get_runtime_defaults(runtime_role))
    secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")
    if secret_key == "dev-secret-key-change-this" and runtime_role in ("web", "worker"):
        logger.warning(
            "SECRET_KEY is using the insecure default value. "
            "Set the SECRET_KEY environment variable to a random string in production."
        )
    app.config.update(
        {
            "ESP_RUNTIME_ROLE": runtime_role,
            "SECRET_KEY": secret_key,
            "SCRAPE_QUEUE_BACKEND": os.environ.get("SCRAPE_QUEUE_BACKEND", "inmemory"),
            "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            "SCRAPE_QUEUE_NAME": os.environ.get("SCRAPE_QUEUE_NAME", "scrape"),
            "RQ_BURST": os.environ.get("RQ_BURST", ""),
            "RQ_WITH_SCHEDULER": os.environ.get("RQ_WITH_SCHEDULER", ""),
            "SCRAPE_JOB_HEARTBEAT_SECONDS": os.environ.get("SCRAPE_JOB_HEARTBEAT_SECONDS", "30"),
            "SCRAPE_JOB_STALL_TIMEOUT_SECONDS": os.environ.get("SCRAPE_JOB_STALL_TIMEOUT_SECONDS", "900"),
            "WORKER_ENABLE_SCHEDULER": os.environ.get("WORKER_ENABLE_SCHEDULER", ""),
            "SCHEDULER_LOCK_BACKEND": os.environ.get("SCHEDULER_LOCK_BACKEND", "auto"),
            "SCHEDULER_LOCK_KEY": os.environ.get("SCHEDULER_LOCK_KEY", "esp:scheduler:lock"),
            "SCHEDULER_LOCK_TTL_SECONDS": os.environ.get("SCHEDULER_LOCK_TTL_SECONDS", "120"),
            "WARM_BROWSER_POOL": os.environ.get("WARM_BROWSER_POOL", ""),
            "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": os.environ.get(
                "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP",
                "0",
            ),
            "WORKER_SELECTOR_REPAIR_LIMIT": os.environ.get("WORKER_SELECTOR_REPAIR_LIMIT", "1"),
            "SELECTOR_REPAIR_MIN_SCORE": os.environ.get("SELECTOR_REPAIR_MIN_SCORE", "90"),
            "SELECTOR_REPAIR_MIN_CANARIES": os.environ.get("SELECTOR_REPAIR_MIN_CANARIES", "2"),
            "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL": os.environ.get(
                "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL",
                "",
            ),
            "SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL": os.environ.get(
                "SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL",
                "",
            ),
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": _as_bool(os.environ.get("SESSION_COOKIE_SECURE", "")),
        }
    )
    if config_overrides:
        app.config.update(config_overrides)
    if runtime_role == "web" and not (config_overrides and "ENABLE_SCHEDULER" in config_overrides):
        app.config["ENABLE_SCHEDULER"] = _resolve_web_scheduler_enabled(app)

    app.secret_key = app.config["SECRET_KEY"]
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    csrf.init_app(app)

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        SessionLocal.remove()

    _register_security_headers(app)
    _register_error_handlers(app)

    _create_scheduler(app)
    if app.config.get("REGISTER_BLUEPRINTS", True):
        _register_blueprints(app)
    if app.config.get("REGISTER_BACKWARD_COMPAT_ALIASES", True):
        _register_backward_compat_aliases(app)
    _register_health_route(app)
    if app.config.get("REGISTER_MEDIA_ROUTE", True):
        _register_media_route(app)
    if app.config.get("REGISTER_CLI_COMMANDS", True):
        _register_cli_commands(app)

    return app


def run_legacy_startup_migrations() -> dict[str, list[str]]:
    """Safely add missing columns to existing tables."""
    from database import apply_additive_startup_migrations

    results = apply_additive_startup_migrations()
    for applied in results.get("applied", []):
        print(f"Migration: Added column {applied}")
    for error in results.get("errors", []):
        print(f"Migration error for {error}")
    return results


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


class _FileSchedulerLockHandle:
    def __init__(self, lock_fd):
        self.lock_fd = lock_fd

    def close(self) -> None:
        if self.lock_fd is not None:
            self.lock_fd.close()
            self.lock_fd = None


class _RedisSchedulerLockHandle:
    def __init__(self, lock, stop_event: threading.Event, renew_thread: threading.Thread):
        self.lock = lock
        self.stop_event = stop_event
        self.renew_thread = renew_thread

    def close(self) -> None:
        self.stop_event.set()
        self.renew_thread.join(timeout=1.0)
        if self.lock is not None:
            try:
                self.lock.release()
            except Exception:
                pass
            self.lock = None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_try_redis_scheduler_lock(app: Flask) -> bool:
    backend = str(app.config.get("SCHEDULER_LOCK_BACKEND", "auto") or "auto").strip().lower()
    if backend == "redis":
        return True
    if backend != "auto":
        return False

    runtime_role = str(app.config.get("ESP_RUNTIME_ROLE", "") or "").strip().lower()
    queue_backend = str(app.config.get("SCRAPE_QUEUE_BACKEND", "inmemory") or "inmemory").strip().lower()

    # Keep today's single-web-service deployment safe: when web still owns the
    # scheduler under the in-memory queue, do not treat the default REDIS_URL as
    # a hard dependency for lock acquisition.
    if runtime_role == "web" and queue_backend != "rq":
        return False
    return True


def _try_acquire_redis_scheduler_lock(app: Flask):
    if not _should_try_redis_scheduler_lock(app):
        return None

    redis_url = str(app.config.get("REDIS_URL", "") or "").strip()
    if not redis_url:
        return None

    try:
        from redis import Redis
    except ImportError:
        return None

    ttl_seconds = max(30, int(app.config.get("SCHEDULER_LOCK_TTL_SECONDS", 120) or 120))
    renew_every_seconds = max(10.0, ttl_seconds / 3)
    lock_key = str(app.config.get("SCHEDULER_LOCK_KEY", "esp:scheduler:lock") or "esp:scheduler:lock")
    try:
        connection = Redis.from_url(redis_url)
        lock = connection.lock(lock_key, timeout=ttl_seconds, blocking=False, thread_local=False)
        if not lock.acquire(blocking=False):
            return False
    except Exception:
        return False

    stop_event = threading.Event()

    def renew_loop() -> None:
        while not stop_event.wait(renew_every_seconds):
            try:
                lock.extend(ttl_seconds)
            except Exception:
                break

    renew_thread = threading.Thread(
        target=renew_loop,
        name="esp-scheduler-lock-renew",
        daemon=True,
    )
    renew_thread.start()
    return _RedisSchedulerLockHandle(lock, stop_event, renew_thread)


def _acquire_scheduler_lock(app: Flask):
    redis_lock_handle = _try_acquire_redis_scheduler_lock(app)
    if redis_lock_handle is False:
        return False, None
    if redis_lock_handle is not None:
        return True, redis_lock_handle

    if _fcntl is None:
        return True, None

    lock_fd = open(_SCHEDULER_LOCK_PATH, "w")
    try:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except (IOError, OSError):
        lock_fd.close()
        return False, None

    return True, _FileSchedulerLockHandle(lock_fd)


def start_scheduler(app: Flask) -> bool:
    scheduler: APScheduler = app.extensions["esp_scheduler"]
    if app.extensions.get("esp_scheduler_started"):
        return True

    acquired, lock_handle = _acquire_scheduler_lock(app)
    if not acquired:
        return False

    try:
        _register_scheduler_jobs(app)
        scheduler.start()
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
        raise

    app.extensions["esp_scheduler_started"] = True
    app.extensions["esp_scheduler_lock_handle"] = lock_handle
    return True


def initialize_app_runtime(app: Flask) -> Flask:
    if app.extensions.get("esp_runtime_initialized"):
        return app

    if app.config.get("RUN_SCHEMA_BOOTSTRAP_ON_STARTUP"):
        with app.app_context():
            applied_schema_mode = bootstrap_schema(app.config.get("SCHEMA_BOOTSTRAP_MODE", "auto"))
            app.extensions["esp_schema_bootstrap_mode"] = applied_schema_mode
            if applied_schema_mode != "disabled" and app.config.get("ENABLE_LEGACY_SCHEMA_PATCHSET"):
                patch_results = run_legacy_startup_migrations() or {}
                patch_errors = list(patch_results.get("errors") or [])
                if patch_errors:
                    raise RuntimeError(
                        "Legacy startup patchset failed: " + "; ".join(str(error) for error in patch_errors)
                    )
            if applied_schema_mode != "disabled" and app.config.get("VERIFY_SCHEMA_DRIFT_ON_STARTUP"):
                schema_snapshot = ensure_additive_schema_ready()
                app.extensions["esp_schema_drift_snapshot"] = schema_snapshot

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
