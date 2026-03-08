import time
import json
import sys

# Force UTF-8 stdout for Windows
sys.stdout.reconfigure(encoding='utf-8')
from mercari_db import scrape_item_detail

# Sample Mercari item URL
URLS = [
    "https://jp.mercari.com/item/m56789324689"
]

results = []
for url in URLS:
    print(f"--- Scraping {url} ---")
    start = time.time()
    try:
        data = scrape_item_detail(url)
        elapsed = time.time() - start
        
        # Save extra timings
        data['elapsed_sec'] = elapsed
        results.append(data)
        
        print(f"Time taken: {elapsed:.2f} seconds")
        print(f"Images count: {len(data.get('image_urls', []))}")
        print(f"Status: {data.get('status')}")
    except Exception as e:
        print(f"Failed: {e}")

with open('scrape_test_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("Saved to scrape_test_results.json")
