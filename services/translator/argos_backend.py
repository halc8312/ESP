"""
Argos Translate backend.

Argos is an offline, MIT-licensed neural MT toolkit built on
CTranslate2 + SentencePiece. We use it as the Phase 1 translator because:

* No network / API key required — safe to run inside the Render worker
  container even on the free tier.
* MIT licence — compatible with commercial use of ESP.
* ja → en model is ~200 MB on disk and keeps a resident footprint of
  roughly 300–400 MB once loaded. Small enough to coexist with the
  scrape browser pool on the ``standard`` worker plan.

The model is loaded lazily on first translation so that importing this
module (or starting the worker process) stays cheap.
"""
from __future__ import annotations

import logging
import threading

from services.translator.base import (
    TranslationError,
    TranslatorUnavailableError,
)
from services.translator.html_segmenter import iter_html_text_segments


logger = logging.getLogger("services.translator.argos")


class ArgosTranslatorBackend:
    """Argos-backed implementation of :class:`TranslatorBackend`."""

    name = "argos"

    def __init__(
        self,
        *,
        source_language: str = "ja",
        target_language: str = "en",
    ) -> None:
        self.source_language = source_language
        self.target_language = target_language
        self._load_lock = threading.Lock()
        self._loaded = False

    # ------------------------------------------------------------------
    # model management
    # ------------------------------------------------------------------

    def _ensure_model_loaded(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            try:
                import argostranslate.package  # type: ignore
                import argostranslate.translate  # type: ignore
            except ImportError as exc:  # pragma: no cover - env misconfig
                raise TranslatorUnavailableError(
                    "argostranslate is not installed"
                ) from exc

            if not self._has_installed_language_pair(argostranslate.translate):
                self._install_language_pair(argostranslate.package)
                if not self._has_installed_language_pair(argostranslate.translate):
                    raise TranslatorUnavailableError(
                        "Argos language pair "
                        f"{self.source_language}->{self.target_language} "
                        "is not installed"
                    )

            logger.info(
                "Argos backend ready for %s->%s",
                self.source_language,
                self.target_language,
            )
            self._loaded = True

    def _has_installed_language_pair(self, argos_translate_module) -> bool:
        try:
            installed_languages = argos_translate_module.get_installed_languages()
        except Exception:  # pragma: no cover - defensive
            return False

        source = next(
            (lang for lang in installed_languages if lang.code == self.source_language),
            None,
        )
        target = next(
            (lang for lang in installed_languages if lang.code == self.target_language),
            None,
        )
        if source is None or target is None:
            return False
        try:
            return source.get_translation(target) is not None
        except Exception:  # pragma: no cover - defensive
            return False

    def _install_language_pair(self, argos_package_module) -> None:
        """Install ja->en from the public index. Best-effort; may fail offline."""
        try:
            argos_package_module.update_package_index()
            available = argos_package_module.get_available_packages()
        except Exception as exc:
            raise TranslatorUnavailableError(
                "Failed to refresh Argos package index; "
                "network access is required for the first-time model download"
            ) from exc

        candidate = next(
            (
                package
                for package in available
                if package.from_code == self.source_language
                and package.to_code == self.target_language
            ),
            None,
        )
        if candidate is None:
            raise TranslatorUnavailableError(
                "Argos does not publish a "
                f"{self.source_language}->{self.target_language} package"
            )

        logger.info(
            "Downloading Argos package %s->%s ...",
            self.source_language,
            self.target_language,
        )
        download_path = candidate.download()
        argos_package_module.install_from_path(download_path)

    # ------------------------------------------------------------------
    # translation
    # ------------------------------------------------------------------

    def translate_plain(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        self._ensure_model_loaded()
        try:
            import argostranslate.translate  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise TranslatorUnavailableError(
                "argostranslate is not installed"
            ) from exc

        try:
            return argostranslate.translate.translate(
                text,
                self.source_language,
                self.target_language,
            )
        except Exception as exc:
            raise TranslationError(
                f"Argos translation failed: {exc}"
            ) from exc

    def translate_html(self, html: str) -> str:
        if not html or not html.strip():
            return ""
        soup, segments = iter_html_text_segments(html)
        if not segments:
            return str(soup)

        for segment in segments:
            translated = self.translate_plain(segment.text)
            segment.apply(translated)

        return str(soup)
