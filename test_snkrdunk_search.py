import sys
import os
sys.path.append(os.getcwd())
from snkrdunk_db import scrape_search_result

print("Testing SNKRDUNK Search...")
search_url = "https://snkrdunk.com/search?keywords=%E3%82%B9%E3%83%8B%E3%83%BC%E3%82%AB%E3%83%BC"
items = scrape_search_result(search_url, max_items=2, headless=True)
if items:
    print(f"Found {len(items)} items from search.")
    for item in items:
        print(f"URL: {item['url']}")
        print(f"Title: {item.get('title')}")
else:
    print("Search failed to return items.")
