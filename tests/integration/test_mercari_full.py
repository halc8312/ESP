import sys
import os
import json
from pprint import pprint

sys.path.append(os.getcwd())
from playwright.sync_api import sync_playwright

from services.patrol.mercari_patrol import MercariPatrol

def test_full():
    print("Finding a live Mercari URL...")
    url = "https://jp.mercari.com/item/m78546740683"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto('https://jp.mercari.com/search?keyword=sneaker', wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            links = page.query_selector_all('a[href*="/item/m"]')
            if links:
                url = 'https://jp.mercari.com' + links[0].get_attribute('href').split('?')[0]
                print('Live URL:', url)
            else:
                print('No links found, using fallback')
            browser.close()
    except Exception as e:
        print(f"Error finding live URL: {e}")
        
    print(f"Scraping {url} with MercariPatrol...")
    patrol = MercariPatrol()
    result = patrol.fetch(url)
    
    print("\n--- PATROL RESULT ---")
    print(f"Success: {result.success}")
    print(f"Status: {result.status}")
    print(f"Price: {result.price}")
    print(f"Error: {result.error}")
    print(f"Reason: {result.reason}")
    print(f"Confidence: {result.confidence}")
    print(f"Price Source: {result.price_source}")

if __name__ == '__main__':
    test_full()
