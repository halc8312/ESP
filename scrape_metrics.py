"""
Scraping metrics and logging utilities.
Provides centralized logging and metrics collection for scraping operations.
"""
import logging
import time
from datetime import datetime
from functools import wraps
from typing import Dict, List, Any, Optional

# Configure scraping-specific logger
scrape_logger = logging.getLogger('scraping')
scrape_logger.setLevel(logging.DEBUG)

# Create handler if not already present
if not scrape_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    scrape_logger.addHandler(handler)


class ScrapeMetrics:
    """Collects and reports scraping metrics."""
    
    def __init__(self):
        self.reset()
    
    def reset(self):
        """Reset all metrics."""
        self.start_time = None
        self.end_time = None
        self.total_attempts = 0
        self.successful = 0
        self.failed = 0
        self.errors: List[str] = []
        self.site = ""
        self.scrape_type = ""  # "search" or "single"
    
    def start(self, site: str, scrape_type: str):
        """Start a scraping session."""
        self.reset()
        self.site = site
        self.scrape_type = scrape_type
        self.start_time = time.time()
        scrape_logger.info(f"[{site}] Starting {scrape_type} scrape session")
    
    def record_attempt(self, success: bool, url: str = "", error: str = ""):
        """Record a single scrape attempt."""
        self.total_attempts += 1
        if success:
            self.successful += 1
            scrape_logger.debug(f"[{self.site}] Success: {url[:50]}...")
        else:
            self.failed += 1
            self.errors.append(f"{url}: {error}")
            scrape_logger.warning(f"[{self.site}] Failed: {url} - {error}")
    
    def finish(self) -> Dict[str, Any]:
        """Finish session and return summary."""
        self.end_time = time.time()
        duration = self.end_time - self.start_time if self.start_time else 0
        
        success_rate = (self.successful / self.total_attempts * 100) if self.total_attempts > 0 else 0
        
        summary = {
            "site": self.site,
            "type": self.scrape_type,
            "duration_seconds": round(duration, 2),
            "total_attempts": self.total_attempts,
            "successful": self.successful,
            "failed": self.failed,
            "success_rate": round(success_rate, 1),
            "errors": self.errors[:5]  # Keep only first 5 errors
        }
        
        # Log summary
        if success_rate < 50 and self.total_attempts > 0:
            scrape_logger.error(
                f"[{self.site}] LOW SUCCESS RATE: {success_rate}% "
                f"({self.successful}/{self.total_attempts}) in {duration:.1f}s"
            )
        else:
            scrape_logger.info(
                f"[{self.site}] Completed: {self.successful}/{self.total_attempts} "
                f"({success_rate}%) in {duration:.1f}s"
            )
        
        return summary


# Global metrics instance for simple use cases
_current_metrics: Optional[ScrapeMetrics] = None


def get_metrics() -> ScrapeMetrics:
    """Get or create the current metrics instance."""
    global _current_metrics
    if _current_metrics is None:
        _current_metrics = ScrapeMetrics()
    return _current_metrics


def log_scrape_result(site: str, url: str, data: Dict[str, Any]) -> bool:
    """
    Log the result of a single item scrape and return success status.
    
    Args:
        site: 'mercari' or 'yahoo'
        url: The URL that was scraped
        data: The scraped data dict
        
    Returns:
        bool: True if scrape was successful (has title)
    """
    success = bool(data.get('title'))
    
    metrics = get_metrics()
    if metrics.start_time is None:
        metrics.start(site, "single")
    
    if success:
        metrics.record_attempt(True, url)
        scrape_logger.info(f"[{site}] Scraped: {data.get('title', 'N/A')[:50]}")
    else:
        error = "No title found"
        if data.get('status') == 'error':
            error = "Page load error"
        metrics.record_attempt(False, url, error)
    
    return success


def check_scrape_health(items: List[Dict]) -> Dict[str, Any]:
    """
    Analyze scrape results and return health report.
    
    Args:
        items: List of scraped item dicts
        
    Returns:
        dict: Health report with warnings if needed
    """
    if not items:
        return {
            "status": "failed",
            "message": "No items scraped - selectors may be broken",
            "action_required": True
        }
    
    # Check for missing fields
    missing_titles = sum(1 for i in items if not i.get('title'))
    missing_prices = sum(1 for i in items if i.get('price') is None)
    missing_images = sum(1 for i in items if not i.get('image_urls'))
    
    total = len(items)
    issues = []
    
    if missing_titles / total > 0.5:
        issues.append(f"Title extraction failing ({missing_titles}/{total})")
    if missing_prices / total > 0.5:
        issues.append(f"Price extraction failing ({missing_prices}/{total})")
    if missing_images / total > 0.5:
        issues.append(f"Image extraction failing ({missing_images}/{total})")
    
    if issues:
        scrape_logger.warning(f"Scrape health issues detected: {', '.join(issues)}")
        return {
            "status": "degraded",
            "message": "; ".join(issues),
            "action_required": True
        }
    
    return {
        "status": "healthy",
        "message": f"All {total} items scraped successfully",
        "action_required": False
    }
