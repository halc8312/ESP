"""
Utility functions for the application.
"""
from urllib.parse import urlsplit, urlunsplit


def normalize_url(raw_url: str) -> str:
    """?以降のクエリを落として正規化したURLを返す"""
    try:
        parts = urlsplit(raw_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return raw_url
