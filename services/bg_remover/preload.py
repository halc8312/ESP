"""
Build-time helper to bake the rembg model into the Docker image.

Running ``python -m services.bg_remover.preload`` downloads the
configured rembg model (default: ``u2netp``) into the rembg cache
directory so the worker never has to hit the network on first use.

If the download cannot complete (e.g. the build environment has no
egress), the script exits with status ``0`` but logs a loud warning so
the worker falls back to the runtime lazy-download path.
"""
from __future__ import annotations

import logging
import os
import sys


def _configured_model_name() -> str:
    value = os.environ.get("BG_REMOVAL_PRELOAD_MODEL") or os.environ.get(
        "BG_REMOVAL_MODEL"
    ) or "u2netp"
    return value.strip() or "u2netp"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[rembg-preload] %(levelname)s %(message)s",
    )
    logger = logging.getLogger("services.bg_remover.preload")

    model_name = _configured_model_name()

    try:
        from rembg.session_factory import new_session  # type: ignore
    except ImportError:
        logger.warning(
            "rembg is not installed; skipping preload. The worker will "
            "raise BackgroundRemoverUnavailableError until the "
            "dependency is added."
        )
        return 0

    try:
        logger.info("Warming rembg model=%s into cache ...", model_name)
        session = new_session(model_name)
        # Touch an attribute so static analysers don't prune the call.
        _ = getattr(session, "inner_session", None)
    except Exception as exc:
        logger.warning(
            "Could not download rembg model %s during build (%s); the "
            "worker will download it lazily on first use.",
            model_name,
            exc,
        )
        return 0

    logger.info("rembg model %s installed successfully.", model_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
