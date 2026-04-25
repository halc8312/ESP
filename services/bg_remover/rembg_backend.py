"""
rembg-powered background-removal backend.

Phase 1 defaults to the ``u2netp`` model (4.7 MB on disk / ~250 MB RAM)
because it comfortably fits alongside the Playwright browser pool on
the current ``esp-worker`` standard plan. The model is loaded lazily on
first use and cached for the lifetime of the process.

Inputs are downscaled to at most ``BG_REMOVAL_MAX_INPUT_DIMENSION``
(default 2000 px on the long side) before inference so a single
oversized product image cannot spike worker RAM above the 2 GB plan
limit.
"""
from __future__ import annotations

import io
import logging
import os
import threading
from typing import Optional

from services.bg_remover.base import (
    BackgroundRemovalError,
    BackgroundRemoverUnavailableError,
)


logger = logging.getLogger("services.bg_remover.rembg")


DEFAULT_MODEL_NAME = "u2netp"
DEFAULT_MAX_INPUT_DIMENSION = 2000


def _configured_model_name() -> str:
    return (os.environ.get("BG_REMOVAL_MODEL") or DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME


def _configured_max_dimension() -> int:
    raw = os.environ.get("BG_REMOVAL_MAX_INPUT_DIMENSION")
    if not raw:
        return DEFAULT_MAX_INPUT_DIMENSION
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INPUT_DIMENSION
    return max(128, value)


class RembgBackgroundRemover:
    """rembg-backed :class:`BackgroundRemover` implementation.

    Thread-safe; a single :class:`rembg.session_factory.Session` is
    shared across calls because building the ONNX session is the most
    expensive part of the pipeline (hundreds of MB on disk and ~1 second
    of CPU).
    """

    name = "rembg"

    def __init__(
        self,
        *,
        model_name: Optional[str] = None,
        max_input_dimension: Optional[int] = None,
    ) -> None:
        self._model_name = (model_name or _configured_model_name()).strip() or DEFAULT_MODEL_NAME
        self._max_input_dimension = max_input_dimension or _configured_max_dimension()
        self._session_lock = threading.Lock()
        self._session = None

    def _resolve_session(self):
        if self._session is not None:
            return self._session
        with self._session_lock:
            if self._session is not None:
                return self._session
            try:
                from rembg.session_factory import new_session  # type: ignore
            except ImportError as exc:
                raise BackgroundRemoverUnavailableError(
                    "rembg is not installed; background removal is unavailable."
                ) from exc
            logger.info("loading rembg session for model=%s", self._model_name)
            self._session = new_session(self._model_name)
            return self._session

    def _downscale_if_needed(self, image_bytes: bytes) -> bytes:
        try:
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise BackgroundRemoverUnavailableError(
                "Pillow is not installed; background removal requires Pillow."
            ) from exc

        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.load()
                width, height = img.size
                longest = max(width, height)
                if longest <= self._max_input_dimension:
                    return image_bytes

                ratio = self._max_input_dimension / float(longest)
                new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
                logger.info(
                    "downscaling input image %sx%s -> %sx%s before bg removal",
                    width,
                    height,
                    new_size[0],
                    new_size[1],
                )
                resized = img.convert("RGBA").resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                resized.save(buf, format="PNG")
                return buf.getvalue()
        except Exception as exc:
            raise BackgroundRemovalError(
                f"could not decode or resize source image: {exc}"
            ) from exc

    def remove_background(self, image_bytes: bytes) -> bytes:
        if not image_bytes:
            raise BackgroundRemovalError("empty source image")

        try:
            from rembg import remove  # type: ignore
        except ImportError as exc:
            raise BackgroundRemoverUnavailableError(
                "rembg is not installed; background removal is unavailable."
            ) from exc

        session = self._resolve_session()
        prepared = self._downscale_if_needed(image_bytes)
        try:
            # ``force_return_bytes`` guarantees a bytes output regardless of
            # input type; without it rembg can return a PIL.Image when the
            # input is a PIL.Image.
            return remove(prepared, session=session, force_return_bytes=True)
        except Exception as exc:
            raise BackgroundRemovalError(
                f"rembg failed to process image: {exc}"
            ) from exc
