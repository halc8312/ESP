"""
Filter Service

Filters out unwanted products based on user-defined exclusion keywords.
"""
import logging
from database import SessionLocal
from models import ExclusionKeyword

logger = logging.getLogger("filter")


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
