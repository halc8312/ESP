"""
Verification script for Yahoo Shopping scraping.
Run this to test if the updated selectors in yahoo_db.py work correctly.
"""
from yahoo_db import scrape_search_result, scrape_single_item
import sys

def test_search(keyword="nintendo switch", limit=2):
    print(f"Testing Yahoo Search Scraping for keyword: '{keyword}'")
    search_url = f"https://shopping.yahoo.co.jp/search?p={keyword.replace(' ', '+')}"
    print(f"Search URL: {search_url}")
    
    items = scrape_search_result(search_url, max_items=limit, headless=True)
    
    if items:
        print(f"\n[SUCCESS] Found {len(items)} items.")
        for i, item in enumerate(items):
            print(f"\n--- Item {i+1} ---")
            print(f"  Title: {item.get('title', 'N/A')}")
            print(f"  Price: {item.get('price', 'N/A')}")
            print(f"  URL: {item.get('url', 'N/A')}")
            print(f"  Images: {len(item.get('image_urls', []))}")
        return True
    else:
        print("\n[FAILURE] No items found.")
        return False

def test_single(url):
    print(f"\nTesting Single Item Scraping for: {url}")
    items = scrape_single_item(url, headless=True)
    if items and items[0].get('title'):
        item = items[0]
        print("[SUCCESS]")
        print(f"  Title: {item.get('title')}")
        print(f"  Price: {item.get('price')}")
        print(f"  Description length: {len(item.get('description', ''))}")
        print(f"  Images: {len(item.get('image_urls', []))}")
        return True
    else:
        print("[FAILURE] Could not scrape item.")
        return False

if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "nintendo switch"
    success = test_search(keyword, limit=2)
    if not success:
        print("\nTrying a direct URL test...")
        test_single("https://store.shopping.yahoo.co.jp/daichugame/switch-sugu-set.html")
