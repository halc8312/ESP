"""
Build-time helper to bake the Argos Translate model into the Docker image.

Running ``python -m services.translator.preload`` downloads the configured
``ja->en`` (or ``TRANSLATOR_PRELOAD_SOURCE_LANG->TRANSLATOR_PRELOAD_TARGET_LANG``)
Argos package and installs it into the shared cache so that the runtime
process never has to hit the network on first use.

If the package index cannot be reached (e.g. the build environment has
no egress), the script exits with status ``0`` but logs a loud warning
so the worker will still fall back to the runtime lazy download path.
"""
from __future__ import annotations

import logging
import os
import sys


def _configured_lang(env_var: str, default: str) -> str:
    value = os.environ.get(env_var) or default
    return value.strip() or default


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[argos-preload] %(levelname)s %(message)s",
    )
    logger = logging.getLogger("services.translator.preload")

    source = _configured_lang("TRANSLATOR_PRELOAD_SOURCE_LANG", "ja")
    target = _configured_lang("TRANSLATOR_PRELOAD_TARGET_LANG", "en")

    try:
        import argostranslate.package  # type: ignore
        import argostranslate.translate  # type: ignore
    except ImportError:
        logger.warning(
            "argostranslate is not installed; skipping preload. The worker "
            "will raise TranslatorUnavailableError until the dependency is added."
        )
        return 0

    try:
        installed = argostranslate.translate.get_installed_languages()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to inspect installed Argos languages: %s", exc)
        installed = []

    source_lang = next((lang for lang in installed if lang.code == source), None)
    target_lang = next((lang for lang in installed if lang.code == target), None)
    if source_lang and target_lang:
        try:
            if source_lang.get_translation(target_lang) is not None:
                logger.info(
                    "Argos %s->%s already installed in build image; skipping download.",
                    source,
                    target,
                )
                return 0
        except Exception:  # pragma: no cover - defensive
            pass

    try:
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()
    except Exception as exc:
        logger.warning(
            "Could not refresh Argos package index (%s); the worker will "
            "download the model lazily on first use.",
            exc,
        )
        return 0

    candidate = next(
        (
            pkg
            for pkg in available
            if pkg.from_code == source and pkg.to_code == target
        ),
        None,
    )
    if candidate is None:
        logger.warning(
            "No Argos package published for %s->%s; skipping preload.",
            source,
            target,
        )
        return 0

    logger.info("Downloading Argos %s->%s package ...", source, target)
    try:
        path = candidate.download()
        argostranslate.package.install_from_path(path)
    except Exception as exc:
        logger.warning(
            "Failed to install Argos %s->%s package during build: %s",
            source,
            target,
            exc,
        )
        return 0

    logger.info("Argos %s->%s package installed successfully.", source, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
