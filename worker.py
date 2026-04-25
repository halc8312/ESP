"""
Dedicated RQ worker entrypoint for Arc 3/C1.
"""
from __future__ import annotations

import logging
import os

from app import create_worker_app
from services.worker_runtime import run_worker


def _env_to_bool_text(env_name: str) -> str:
    return os.environ.get(env_name, "")


def _configure_logging() -> None:
    raw_level = str(os.environ.get("LOG_LEVEL", "INFO") or "INFO").strip().upper()
    level = getattr(logging, raw_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    _configure_logging()
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
            "ENABLE_SCHEDULER": _env_to_bool_text("WORKER_ENABLE_SCHEDULER"),
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
    return run_worker(app)


if __name__ == "__main__":
    raise SystemExit(main())
