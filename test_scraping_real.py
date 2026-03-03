import concurrent.futures
import logging
import time
import json
import os
from datetime import datetime

# Import scrapers
import mercari_db
import yahoo_db
import rakuma_db
import surugaya_db
import offmall_db
import yahuoku_db
import snkrdunk_db

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ScrapingTest")

# Test URLs - Representative items from each store (Updated 2026-03-03)
TEST_CONFIG = {
    "mercari": {
        "url": "https://jp.mercari.com/item/m53054743401",
        "module": mercari_db,
    },
    "mercari_shops": {
        "url": "https://jp.mercari.com/shops/product/xhF79ibHz3KwucSXev32C5",
        "module": mercari_db,
    },
    "yahoo": {
        "url": "https://store.shopping.yahoo.co.jp/nextfreedom/igress-008.html",
        "module": yahoo_db,
    },
    "rakuma": {
        "url": "https://item.fril.jp/fed8b046583deff9f860ce320f64633c",
        "module": rakuma_db,
    },
    "surugaya": {
        "url": "https://www.suruga-ya.jp/product/detail/109003813",
        "module": surugaya_db,
    },
    "offmall": {
        "url": "https://netmall.hardoff.co.jp/product/5966534/",
        "module": offmall_db,
    },
    "yahuoku": {
        "url": "https://page.auctions.yahoo.co.jp/auction/g1221465796",
        "module": yahuoku_db,
    },
    "snkrdunk": {
        "url": "https://snkrdunk.com/products/CT8013-170",
        "module": snkrdunk_db,
    }
}

def check_result(site, data):
    """Verify the scraped data contains all required elements."""
    errors = []
    
    # Title
    if not data.get("title") or len(data["title"]) < 2:
        errors.append("Title missing or too short")
    
    # Price
    price = data.get("price")
    if price is None:
        errors.append("Price is None")
    elif not isinstance(price, (int, float)) or price <= 0:
        errors.append(f"Invalid price: {price}")
        
    # Description
    desc = data.get("description", "")
    if not desc or len(desc) < 10:
        # Some sites might have short descriptions, but usually they are longer
        if site not in ["rakuma"]: # Rakuma descriptions can be brief sometimes
            errors.append("Description missing or too short")
            
    # Images
    images = data.get("image_urls", [])
    if not images:
        errors.append("No images found")
    else:
        # Check if they look like URLs
        if not any(img.startswith("http") for img in images):
            errors.append("Invalid image URLs")
            
    # Status
    status = data.get("status")
    if not status or status not in ["on_sale", "sold", "active", "draft", "unknown", "blocked"]:
        errors.append(f"Invalid status: {status}")
        
    return errors

def run_test(site, config):
    url = config["url"]
    module = config["module"]
    
    logger.info(f"STARTING TEST for {site}: {url}")
    start_time = time.time()
    
    try:
        # All modules have scrape_single_item which returns a list[dict]
        results = module.scrape_single_item(url, headless=True)
        duration = time.time() - start_time
        
        if not results:
            logger.error(f"FAILED {site}: No data returned")
            return {
                "site": site,
                "url": url,
                "success": False,
                "errors": ["No data returned"],
                "duration": duration,
                "data": None
            }
            
        data = results[0]
        errors = check_result(site, data)
        
        if errors:
            logger.warning(f"PARTIAL FAILURE {site}: {errors}")
            return {
                "site": site,
                "url": url,
                "success": False,
                "errors": errors,
                "duration": duration,
                "data": data
            }
        else:
            logger.info(f"SUCCESS {site} in {duration:.2f}s")
            return {
                "site": site,
                "url": url,
                "success": True,
                "errors": [],
                "duration": duration,
                "data": data
            }
            
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"CRITICAL ERROR {site}: {str(e)}")
        return {
            "site": site,
            "url": url,
            "success": False,
            "errors": [f"Exception: {str(e)}"],
            "duration": duration,
            "data": None
        }

def main():
    print("=== ESP COMPREHENSIVE SCRAPING TEST ===")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Stores to test: {', '.join(TEST_CONFIG.keys())}")
    print("Executing in parallel...")
    print("-" * 50)
    
    results_summary = []
    
    # Sort config to potentially run them in a more predictable order or just run them
    site_configs = list(TEST_CONFIG.items())
    
    # We'll run them one by one or with a staggered start to avoid WebDriverManager race conditions
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(TEST_CONFIG))) as executor:
        futures = []
        for site, config in site_configs:
            futures.append(executor.submit(run_test, site, config))
            time.sleep(2) # Staggered start
        
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                results_summary.append(res)
            except Exception as exc:
                print(f"A task generated an exception: {exc}")
                
    print("\n" + "=" * 50)
    print("TEST RESULTS SUMMARY")
    print("-" * 50)
    
    total = len(results_summary)
    passed = sum(1 for r in results_summary if r["success"])
    
    for r in results_summary:
        status = "PASS" if r["success"] else "FAIL"
        print(f"[{status}] | {r['site']:<15} | {r['duration']:>5.2f}s | {r['url']}")
        if r["errors"]:
            for err in r["errors"]:
                print(f"      - {err}")
    
    print("-" * 50)
    print(f"TOTAL: {total} | PASSED: {passed} | FAILED: {total - passed}")
    print("=" * 50)
    
    # Save results to a report file
    report = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed
        },
        "details": results_summary
    }
    
    with open("scraping_test_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"Detailed report saved to scraping_test_report.json")

if __name__ == "__main__":
    main()
