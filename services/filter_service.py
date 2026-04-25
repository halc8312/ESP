"""
Filter Service

Filters out unwanted products based on user-defined exclusion keywords
and numeric price ranges.
"""
import logging
from database import SessionLocal
from models import ExclusionKeyword

logger = logging.getLogger("filter")


def _coerce_price_value(value):
    """Convert a raw price-like value to int, or None when unavailable."""
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        numeric_value = int(value)
        return numeric_value if numeric_value >= 0 else None

    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None

    numeric_value = int(digits)
    return numeric_value if numeric_value >= 0 else None


def normalize_price_bounds(price_min=None, price_max=None) -> tuple:
    """
    Normalize optional price bounds.

    Returns:
        (min_value, max_value) as integers or None.
        When min > max, the values are swapped.
    """
    min_value = _coerce_price_value(price_min)
    max_value = _coerce_price_value(price_max)

    if min_value is not None and max_value is not None and min_value > max_value:
        min_value, max_value = max_value, min_value

    return min_value, max_value


def get_user_exclusion_keywords(user_id: int) -> list:
    """
    Get all exclusion keywords for a user.
    
    Returns:
        List of tuples: [(keyword, match_type), ...]
    """
    session = SessionLocal()
    try:
        keywords = session.query(ExclusionKeyword).filter_by(user_id=user_id).all()
        return [(k.keyword.lower(), k.match_type) for k in keywords]
    finally:
        session.close()


def is_excluded(title: str, keywords: list) -> bool:
    """
    Check if a title matches any exclusion keyword.
    
    Args:
        title: Product title to check
        keywords: List of (keyword, match_type) tuples
        
    Returns:
        True if excluded, False if allowed
    """
    if not title or not keywords:
        return False
    
    title_lower = title.lower()
    
    for keyword, match_type in keywords:
        if match_type == "exact":
            # Exact match (title equals keyword)
            if title_lower == keyword:
                return True
        else:
            # Partial match (keyword is contained in title)
            if keyword in title_lower:
                return True
    
    return False


def filter_excluded_items(items: list, user_id: int) -> tuple:
    """
    Filter out items that match user's exclusion keywords.
    
    Args:
        items: List of scraped item dictionaries
        user_id: User ID to get exclusion keywords for
        
    Returns:
        Tuple of (filtered_items, excluded_count)
    """
    if not items:
        return items, 0
    
    keywords = get_user_exclusion_keywords(user_id)
    if not keywords:
        return items, 0
    
    filtered = []
    excluded_count = 0
    
    for item in items:
        title = item.get("title", "")
        if is_excluded(title, keywords):
            logger.info(f"Excluded: {title[:50]}...")
            excluded_count += 1
        else:
            filtered.append(item)
    
    if excluded_count > 0:
        logger.info(f"Filtered {excluded_count} items based on exclusion keywords")
    
    return filtered, excluded_count


def filter_items_by_price(items: list, price_min=None, price_max=None) -> tuple:
    """
    Filter scraped items by numeric price range.

    Items with missing/unparseable prices are excluded when any price bound is set,
    because they cannot be verified against the requested range.
    """
    min_value, max_value = normalize_price_bounds(price_min, price_max)
    if min_value is None and max_value is None:
        return items, 0

    filtered = []
    excluded_count = 0

    for item in items:
        price_value = _coerce_price_value(item.get("price"))
        if price_value is None:
            excluded_count += 1
            continue
        if min_value is not None and price_value < min_value:
            excluded_count += 1
            continue
        if max_value is not None and price_value > max_value:
            excluded_count += 1
            continue
        filtered.append(item)

    if excluded_count > 0:
        logger.info(
            "Filtered %s items outside requested price range min=%s max=%s",
            excluded_count,
            min_value,
            max_value,
        )

    return filtered, excluded_count
