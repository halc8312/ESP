import sys
import os
import json
import logging

logging.basicConfig(level=logging.DEBUG)

sys.path.append(os.getcwd())
try:
    from services.scraping_client import fetch_static
    from yahoo_db import _extract_item_from_page, scrape_item_detail_light
    
    url = "https://store.shopping.yahoo.co.jp/ryouhin-boueki/sps501-1.html?nodeeplink=0"
    print(f"Fetching {url}")
    page = fetch_static(url)
    
    script_el = page.find("#__NEXT_DATA__")
    print(f"__NEXT_DATA__ exists: {bool(script_el)}")
    
    item = _extract_item_from_page(page)
    print(f"Item from __NEXT_DATA__: {bool(item)}")
    if item:
        print(f"Keys in item: {list(item.keys())}")
        
    result = scrape_item_detail_light(url)
    print(f"Final scrape result:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
except Exception as e:
    import traceback
    traceback.print_exc()
