from bs4 import BeautifulSoup

with open('dump.html', 'rb') as f:
    html = f.read()

soup = BeautifulSoup(html, 'html.parser')

title = soup.select("h1.product-name-en")
title2 = soup.select("p.product-name-jp")
title3 = soup.select("h1")

print(f"h1.product-name-en: {title[0].text.strip() if title else 'None'}")
print(f"p.product-name-jp: {title2[0].text.strip() if title2 else 'None'}")
print(f"h1: {title3[0].text.strip() if title3 else 'None'}")

price = soup.select("span.product-lowest-price")
print(f"span.product-lowest-price: {price[0].text.strip() if price else 'None'}")

desc = soup.select("div.product-acd-content.product-content-info-detail")
print(f"description: {desc[0].text.strip() if desc else 'None'}")

imgs = soup.select(".product-img img")
print(f"images count: {len(imgs)}")
if imgs:
    print(f"first img src: {imgs[0].get('src')}")
