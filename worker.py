"""
Dedicated RQ worker entrypoint for Arc 3/C1.
"""
from __future__ import annotations

import logging
import os

from app import create_worker_app, get_scheduler_health_snapshot
from services.worker_runtime import run_worker
from utils.env_helpers import env_flag


logger = logging.getLogger("worker_entrypoint")


def _env_to_bool(env_name: str) -> bool:
    return env_flag(env_name)


def _configure_logging() -> None:
    raw_level = str(os.environ.get("LOG_LEVEL", "INFO") or "INFO").strip().upper()
    level = getattr(logging, raw_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    _configure_logging()
    logger.info(
        "Worker entrypoint starting: queue_backend=%s scheduler_env=%s",
        os.environ.get("SCRAPE_QUEUE_BACKEND", "rq"),
        os.environ.get("WORKER_ENABLE_SCHEDULER", ""),
    )
    os.environ.setdefault("ENABLE_SHARED_BROWSER_RUNTIME", "1")
    os.environ.setdefault("BROWSER_POOL_WARM_SITES", "mercari")
    os.environ.setdefault("MERCARI_USE_BROWSER_POOL_DETAIL", "1")
    os.environ.setdefault("MERCARI_PATROL_USE_BROWSER_POOL", "1")
    os.environ.setdefault("SNKRDUNK_USE_BROWSER_POOL_DYNAMIC", "1")
    app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": os.environ.get("SCRAPE_QUEUE_BACKEND", "rq"),
            "RQ_BURST": os.environ.get("RQ_BURST", ""),
            "RQ_WITH_SCHEDULER": os.environ.get("RQ_WITH_SCHEDULER", ""),
            "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": True,
            "SCHEMA_BOOTSTRAP_MODE": os.environ.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
            "ENABLE_LEGACY_SCHEMA_PATCHSET": True,
            "VERIFY_SCHEMA_DRIFT_ON_STARTUP": True,
            "ENABLE_SCHEDULER": _env_to_bool("WORKER_ENABLE_SCHEDULER"),
            "WARM_BROWSER_POOL": os.environ.get("WARM_BROWSER_POOL", "1"),
            "WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP": os.environ.get(
                "WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP",
                "1",
            ),
            "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": os.environ.get(
                "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP",
                "0",
            ),
            "WORKER_SELECTOR_REPAIR_LIMIT": os.environ.get(
                "WORKER_SELECTOR_REPAIR_LIMIT",
                "1",
            ),
        }
    )
    scheduler_enabled = getattr(app, "config", {}).get("ENABLE_SCHEDULER", False)
    scheduler_health = get_scheduler_health_snapshot(app) if hasattr(app, "extensions") else {}
    logger.info(
        "Worker app created: scheduler_enabled=%s scheduler_health=%s",
        scheduler_enabled,
        scheduler_health,
    )
    return run_worker(app)


if __name__ == "__main__":
    raise SystemExit(main())
