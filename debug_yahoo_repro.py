from yahoo_db import scrape_search_result, scrape_single_item

def test_search():
    print("Testing Yahoo Search Scraping...")
    # Search for a common item
    search_url = "https://shopping.yahoo.co.jp/search?p=nintendo+switch"
    items = scrape_search_result(search_url, max_items=1, headless=True)
    if items:
        print(f"Search successful! Found {len(items)} items.")
        print(f"First item title: {items[0].get('title')}")
        return items[0]['url']
    else:
        print("Search failed. No items found.")
        return None

def test_single(url):
    print(f"\nTesting Single Item Scraping for {url}...")
    items = scrape_single_item(url, headless=True)
    if items:
        item = items[0]
        print("Scrape Result:")
        print(f"Title: {item.get('title')}")
        print(f"Price: {item.get('price')}")
        print(f"Description found: {bool(item.get('description'))}")
        print(f"Images found: {len(item.get('image_urls'))}")
    else:
        print("Single item scrape failed.")

if __name__ == "__main__":
    url = test_search()
    if url:
        test_single(url)
    else:
        # Fallback URL if search fails (this might be stale but worth checking if method exists)
        pass
