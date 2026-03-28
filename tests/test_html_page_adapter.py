from services.html_page_adapter import HtmlPageAdapter


def test_html_page_adapter_supports_css_text_and_attributes():
    page = HtmlPageAdapter(
        """
        <html>
          <head><title>Sample</title></head>
          <body>
            <h1 data-testid="name">Mercari Item</h1>
            <img src="https://static.mercdn.net/item/photos/1.jpg" />
          </body>
        </html>
        """,
        url="https://jp.mercari.com/item/m1",
        status=200,
    )

    title_nodes = page.css("h1")
    image_nodes = page.css("img")

    assert page.url == "https://jp.mercari.com/item/m1"
    assert page.status == 200
    assert title_nodes[0].text == "Mercari Item"
    assert title_nodes[0].attrib["data-testid"] == "name"
    assert image_nodes[0].attrib["src"].startswith("https://static.mercdn.net/")
