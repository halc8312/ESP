"""
Translator package.

Public surface for translating product title / description text into English
as part of the product-edit workflow. The backend is swappable via
``TRANSLATOR_BACKEND`` so that Phase 2 can adopt a paid API (e.g. DeepL)
without changing callers.
"""
from services.translator.base import (
    TranslationError,
    TranslatorBackend,
    TranslatorUnavailableError,
)
from services.translator.registry import (
    get_translator_backend,
    resolve_backend_name,
)
from services.translator.source_hash import (
    compute_source_hash,
    normalize_source_for_hash,
)

__all__ = [
    "TranslationError",
    "TranslatorBackend",
    "TranslatorUnavailableError",
    "get_translator_backend",
    "resolve_backend_name",
    "compute_source_hash",
    "normalize_source_for_hash",
]
