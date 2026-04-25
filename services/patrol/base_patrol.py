"""
Base class for lightweight patrol scrapers.
These scrapers only fetch price and stock data for efficient monitoring.
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import logging

from services.scrape_alerts import report_patrol_result

logger = logging.getLogger("patrol")


class PatrolResult:
    """Result from a patrol scrape."""
    
    def __init__(
        self,
        price: Optional[int] = None,
        status: str = "unknown",
        variants: Optional[List[Dict]] = None,
        error: Optional[str] = None,
        confidence: str = "high",
        reason: Optional[str] = None,
        price_source: Optional[str] = None,
        evidence_strength: str = "none",
    ):
        self.price = price
        self.status = status  # "active", "sold", "deleted", "unknown"
        self.variants = variants or []
        self.error = error
        self.confidence = confidence
        self.reason = reason
        self.price_source = price_source
        self.evidence_strength = evidence_strength  # "hard", "soft", "none"
        self.success = error is None
    
    def __repr__(self):
        return (
            "PatrolResult("
            f"price={self.price}, status={self.status}, "
            f"confidence={self.confidence}, variants={len(self.variants)})"
        )


class BasePatrol(ABC):
    """
    Abstract base class for lightweight patrol scrapers.
    Subclasses implement site-specific price/stock extraction.
    """
    
    @abstractmethod
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and stock data from a product URL.
        
        Args:
            url: Product page URL
            driver: Optional shared WebDriver instance
            
        Returns:
            PatrolResult with price, status, and variants
        """
        pass
    
    @staticmethod
    def parse_price(price_str: str) -> Optional[int]:
        """Extract integer price from string like '¥1,980' or '1980円'."""
        if not price_str:
            return None
        import re
        # Remove currency symbols, commas, spaces
        cleaned = re.sub(r'[¥円,\s]', '', price_str)
        try:
            return int(cleaned)
        except ValueError:
            return None

    def _finalize_result(self, site: str, url: str, result: PatrolResult, *, page_type: str = "patrol_detail") -> PatrolResult:
        report_patrol_result(site, url, result, page_type=page_type)
        return result
