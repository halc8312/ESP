from lxml import html
import sys
sys.stdout.reconfigure(encoding='utf-8')

tree = html.parse('mercari_page_dump_live.html')

h1 = tree.xpath('//h1')[0]
print("H1 text:", h1.text_content().strip())

# Look at the siblings or parents
parent = h1.getparent()
while parent is not None and len(parent.text_content()) < 500:
    parent = parent.getparent()

# Now we have a container that holds title, price, and maybe description
print("CONTAINER TAG:", parent.tag, "CLASS:", parent.attrib.get('class'))
for child in parent.iterdescendants():
    text = child.text_content().strip()
    if text and len(text) < 100:
        if '¥' in text or '円' in text:
            print("  PRICE =>", child.tag, "CLASS:", child.attrib.get('class'), "TEXT:", text[:30].replace('\n', ' '))
        elif child.tag == 'p' or child.tag == 'div' or child.tag == 'span':
             # don't print too much
             pass

# For description, it's usually a large text block.
# Let's find all divs with long text
for div in tree.xpath('//div'):
    text = div.text_content().strip()
    if len(text) > 50 and "商品の説明" not in text and div.attrib.get('id') != 'item-info':
        # Let's find the innermost div with long text
        if not any(len(child.text_content().strip()) > 50 for child in div):
            if "NIKE" in text or "ダンク" in text:
                print("DESC CANDIDATE => CLASS:", div.attrib.get('class'), "TEXT:", text[:100].replace('\n', ' '))
