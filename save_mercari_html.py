import sys
import os
sys.path.append(os.getcwd())
from services.scraping_client import fetch_dynamic

print("Fetching https://jp.mercari.com/item/m78546740683...")
page = fetch_dynamic("https://jp.mercari.com/item/m78546740683", headless=True, network_idle=True)
with open("mercari_test.html", "wb") as f:
    f.write(page.body)
print("Saved to mercari_test.html")
