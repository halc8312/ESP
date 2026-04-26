import sys
import os
import sqlite3
sys.path.append(os.getcwd())
from scrapling import StealthyFetcher
from services.patrol.mercari_patrol import MercariPatrol

def find_active_mercari_link():
    print("Fetching Mercari Sneakers...")
    page = StealthyFetcher.fetch("https://jp.mercari.com/search?keyword=sneaker", headless=True, network_idle=True)
    links = []
    
    # Let's inspect data-testid="item-cell"
    item_cells = page.css('li[data-testid="item-cell"] a')
    for cell in item_cells:
        href = cell.attrib.get('href')
        if href and "/item/m" in href:
            links.append(href)
            
    if not links:
        print("No item links found with li[data-testid='item-cell'] a. Trying raw href...")
        links = [a.attrib.get('href') for a in page.css('a') if a.attrib.get('href') and '/item/m' in a.attrib.get('href')]
        
    print(f"Found {len(links)} links. First link: {links[0] if links else 'None'}")
    return links[0] if links else None

def main():
    url = find_active_mercari_link()
    if url:
        full_url = "https://jp.mercari.com" + url if url.startswith('/') else url
        print(f"Testing patrol on {full_url}")
        patrol = MercariPatrol()
        result = patrol.fetch(full_url)
        print("Patrol Result:", result)


if __name__ == "__main__":
    main()
