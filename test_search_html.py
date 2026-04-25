import sys
import os
sys.path.append(os.getcwd())
from scrapling import StealthyFetcher

def main():
    print("Fetching Mercari Sneakers...")
    page = StealthyFetcher.fetch("https://jp.mercari.com/search?keyword=sneaker", headless=True, network_idle=True)

    html = page.body.decode('utf-8', errors='ignore')
    print(f"HTML size: {len(html)}")

    links = page.css('a[href*="/item/"]')
    print(f"Found {len(links)} links using a[href*='/item/']")

    with open("search_dump.html", "w", encoding='utf-8') as f:
        f.write(html)
    print("Saved search_dump.html")


if __name__ == "__main__":
    main()
