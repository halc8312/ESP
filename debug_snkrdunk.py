import sys
import os
from snkrdunk_db import scrape_search_result, scrape_single_item

def test_snkrdunk():
    print("Testing SNKRDUNK Search...")
    search_url = "https://snkrdunk.com/search?keywords=スニーカー"
    items = scrape_search_result(search_url, max_items=2, headless=True)
    if items:
        print(f"Found {len(items)} items from search.")
        for item in items:
            print(f"URL: {item['url']}")
            test_single(item['url'])
    else:
        print("Search failed.")

def test_single(url):
    print(f"\nTesting SNKRDUNK Single Item: {url}")
    items = scrape_single_item(url, headless=True)
    if items:
        item = items[0]
        print(f"Title: {item.get('title')}")
        print(f"Price: {item.get('price')}")
        print(f"Description: {bool(item.get('description'))}")
        print(f"Images: {len(item.get('image_urls', []))}")
    else:
        print("Single scrape failed.")

if __name__ == "__main__":
    test_snkrdunk()
