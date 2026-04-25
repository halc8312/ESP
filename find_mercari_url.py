from playwright.sync_api import sync_playwright
import time

def get_url():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto('https://jp.mercari.com/search?keyword=sneaker', wait_until='networkidle')
        time.sleep(3)
        links = page.query_selector_all('a[href*="/item/m"]')
        if links:
            url = 'https://jp.mercari.com' + links[0].get_attribute('href').split('?')[0]
            print('Live_URL_Found:', url)
        else:
            print('No links found')
        browser.close()

if __name__ == '__main__':
    get_url()
