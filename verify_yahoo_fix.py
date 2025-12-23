from yahoo_db import scrape_item_detail
from mercari_db import create_driver
import sys

def verify_fix(url):
    print(f"Verifying Yahoo Scraping for: {url}")
    driver = create_driver(headless=True)
    try:
        data = scrape_item_detail(driver, url)
        print("--- Scraping Result ---")
        print(f"Title: {data.get('title')}")
        print(f"Price: {data.get('price')}")
        print(f"Description length: {len(data.get('description', ''))}")
        print(f"Images: {len(data.get('image_urls', []))}")
        
        if data.get('title') and data.get('price'):
             print("\n[SUCCESS] Basic data found.")
        else:
             print("\n[FAILURE] Title or Price missing.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        # Default to the one investigated
        url = "https://store.shopping.yahoo.co.jp/daichugame/switch-sugu-set.html?nodeeplink=0"
    verify_fix(url)
