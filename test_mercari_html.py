import sys
import asyncio
sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import async_playwright

async def run():
    url = "https://jp.mercari.com/item/m56789324689"
    print(f"Fetching {url} with async_playwright...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"}
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        
        html = await page.content()
        print(f"Length of HTML: {len(html)}")
        with open("mercari_page_dump_live.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Saved to mercari_page_dump_live.html")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
