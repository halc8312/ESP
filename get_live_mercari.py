from playwright.sync_api import sync_playwright
import time
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('https://jp.mercari.com/search?keyword=sneaker', wait_until='domcontentloaded')
    time.sleep(3)
    links = page.query_selector_all('a[href*="/item/m"]')
    if links:
        url = 'https://jp.mercari.com' + links[0].get_attribute('href').split('?')[0]
        print('Live URL:', url)
    else:
        print('No links found')
    browser.close()
