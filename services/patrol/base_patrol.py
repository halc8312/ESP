"""
Base class for lightweight patrol scrapers.
These scrapers only fetch price and stock data for efficient monitoring.
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import logging

logger = logging.getLogger("patrol")


class PatrolResult:
    """Result from a patrol scrape."""
    
    def __init__(
        self,
        price: Optional[int] = None,
        status: str = "unknown",
        variants: Optional[List[Dict]] = None,
        error: Optional[str] = None
    ):
        self.price = price
        self.status = status  # "active", "sold", "deleted", "unknown"
        self.variants = variants or []
        self.error = error
        self.success = error is None
    
    def __repr__(self):
        return f"PatrolResult(price={self.price}, status={self.status}, variants={len(self.variants)})"


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
