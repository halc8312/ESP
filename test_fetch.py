import sys
import os
sys.path.append(os.getcwd())
from services.scraping_client import fetch_static
url = "https://snkrdunk.com/products/HM4740-001"
print(f"Fetching {url}")
try:
    page = fetch_static(url)
    print(f"Status: {page.status}")
    title_matches = page.css('title')
    title = title_matches[0].text if title_matches else 'No title'
    print(f"Title: {title}")
    
    body_text = str(page.body or b"")
    if "Cloudflare" in body_text or "Just a moment" in body_text:
        print("BLOCKED BY CLOUDFLARE")
    else:
        print("Not blocked by Cloudflare (based on string matching)")
        
    with open('dump.html', 'wb') as f:
        f.write(page.body or b"")
    print("Saved to dump.html")
except Exception as e:
    print(f"Fetch failed: {e}")
