import sys
sys.stdout.reconfigure(encoding='utf-8')
from scrapling import StealthyFetcher

url = "https://jp.mercari.com/item/m56789324689"
print("Fetching...")
page = StealthyFetcher.fetch(url, headless=True, network_idle=False)
print("Has get_text?", hasattr(page, 'get_text'))
print("Has text property?", hasattr(page, 'text'))
print("Has body?", hasattr(page, 'body'))

try:
    print("page.text length:", len(page.text))
except: pass

try:
    h1 = page.css("h1")[0]
    print("H1 text:", h1.text)
except: pass

try:
    body = page.css("body")[0]
    print("Body text length:", len(body.text))
except: pass
