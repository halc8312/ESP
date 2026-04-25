"""
Minimal CSS-queryable HTML adapter backed by BeautifulSoup.
"""
from __future__ import annotations

from bs4 import BeautifulSoup


def _normalize_attr_value(value) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    return str(value or "")


class HtmlNodeAdapter:
    def __init__(self, element) -> None:
        self._element = element

    @property
    def text(self) -> str:
        try:
            return self._element.get_text(" ", strip=True)
        except Exception:
            return ""

    @property
    def attrib(self) -> dict[str, str]:
        try:
            return {
                str(key): _normalize_attr_value(value)
                for key, value in dict(self._element.attrs or {}).items()
            }
        except Exception:
            return {}


class HtmlPageAdapter:
    def __init__(self, html: str, *, url: str = "", status: int = 200) -> None:
        self.body = html or ""
        self.url = str(url or "")
        self.status = int(status or 200)
        self._soup = BeautifulSoup(self.body, "html.parser")

    def css(self, selector: str) -> list[HtmlNodeAdapter]:
        try:
            return [HtmlNodeAdapter(element) for element in self._soup.select(selector)]
        except Exception:
            return []

    def find(self, selector: str):
        matches = self.css(selector)
        return matches[0] if matches else None

    def get_text(self) -> str:
        try:
            return self._soup.get_text(" ", strip=True)
        except Exception:
            return ""

    def get_all_text(self) -> str:
        return self.get_text()
