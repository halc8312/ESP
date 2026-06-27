import sys
import os
sys.path.append(os.getcwd())

from scrapling import StealthyFetcher
from services.patrol.mercari_patrol import MercariPatrol

def test_fetch_active():
    search_url = "https://jp.mercari.com/search?keyword=sneaker"
    print(f"Fetching search page: {search_url}")
    page = StealthyFetcher.fetch(search_url, headless=True, network_idle=True)
    
    links = []
    # Both 'a[href*="/item/m"]' and 'li[data-testid="item-cell"] a'
    for element in page.css('a[href*="/item/m"]'):
        links.append(element.attrib.get('href'))
        
    print(f"Found {len(links)} item links")
    if not links:
        print("HTML snippet of search page:")
        print(page.body[:1000].decode('utf-8', errors='ignore'))
        return

    patrol = MercariPatrol()
    for link in links[:3]:
        full_url = "https://jp.mercari.com" + link.split('?')[0] if link.startswith('/') else link
        print(f"\n--- Testing {full_url} ---")
        result = patrol.fetch(full_url)
        print(f"Success: {result.success}")
        print(f"Status: {result.status}")
        print(f"Price: {result.price}")
        print(f"Error: {result.error}")
        print(f"Reason: {result.reason}")

if __name__ == '__main__':
    test_fetch_active()
