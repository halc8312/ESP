"""
Selector configuration loader for scraping modules.
Provides centralized access to CSS selectors defined in config/scraping_selectors.json.
"""
import json
import os
import logging

_selectors_cache = None
_config_path = os.path.join(os.path.dirname(__file__), 'config', 'scraping_selectors.json')

def load_selectors(force_reload: bool = False) -> dict:
    """
    Load selectors from the configuration file.
    Results are cached for performance.
    
    Args:
        force_reload: If True, reload from file even if cached
        
    Returns:
        dict: Selector configuration
    """
    global _selectors_cache
    
    if _selectors_cache is not None and not force_reload:
        return _selectors_cache
    
    try:
        with open(_config_path, 'r', encoding='utf-8') as f:
            _selectors_cache = json.load(f)
            logging.info(f"Loaded selectors from {_config_path}")
            return _selectors_cache
    except FileNotFoundError:
        logging.warning(f"Selector config not found at {_config_path}, using empty config")
        return {}
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse selector config: {e}")
        return {}


def get_selectors(site: str, page_type: str, field: str) -> list:
    """
    Get selectors for a specific site, page type, and field.
    
    Args:
        site: 'mercari' or 'yahoo'
        page_type: 'detail', 'search', 'shops', 'general'
        field: 'title', 'price', 'description', 'images', 'item_links', etc.
        
    Returns:
        list: List of CSS selectors to try in order, empty list if not found
        
    Example:
        >>> get_selectors('yahoo', 'detail', 'title')
        ["[class*='styles_itemName']", "[class*='styles_itemTitle']", ...]
    """
    config = load_selectors()
    try:
        return config.get(site, {}).get(page_type, {}).get(field, [])
    except Exception:
        return []


def get_valid_domains(site: str, page_type: str = 'search') -> list:
    """
    Get valid domains for URL matching.
    
    Args:
        site: 'mercari' or 'yahoo'
        page_type: usually 'search'
        
    Returns:
        list: List of valid domain substrings
    """
    config = load_selectors()
    try:
        return config.get(site, {}).get(page_type, {}).get('valid_domains', [])
    except Exception:
        return []
