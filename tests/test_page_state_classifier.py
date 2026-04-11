import json
from pathlib import Path

from services.html_page_adapter import HtmlPageAdapter
from services.page_state_classifier import classify_page_state


def test_classify_mercari_deleted_fixture_disallows_healing():
    fixture_path = Path(__file__).resolve().parents[1] / "mercari_page_dump.html"
    page_url = "https://jp.mercari.com/item/m71383569733"
    html = fixture_path.read_text(encoding="utf-8")
    page = HtmlPageAdapter(html, url=page_url)

    assessment = classify_page_state("mercari", page, page_type="detail")

    assert assessment.state == "deleted"
    assert assessment.allow_healing is False
    assert any(reason.startswith("missing-marker:") for reason in assessment.reasons)


def test_classify_mercari_detail_with_product_signals_allows_healing():
    page = HtmlPageAdapter(
        """
        <html>
          <head>
            <title>Mercari Item</title>
            <meta name="product:price:amount" content="12000">
          </head>
          <body>
            <main>
              <h1>Mercari Product Title</h1>
              <button>購入手続きへ</button>
            </main>
          </body>
        </html>
        """,
        url="https://jp.mercari.com/item/m123456789",
    )

    assessment = classify_page_state("mercari", page, page_type="detail")

    assert assessment.state == "healthy"
    assert assessment.allow_healing is True


def test_classify_snkrdunk_login_gate_disallows_healing():
    page = HtmlPageAdapter(
        """
        <html>
          <head><title>SNKRDUNK Login</title></head>
          <body>
            <form action="/login">
              <label>ログイン</label>
              <input type="email" name="email">
              <input type="password" name="password">
            </form>
          </body>
        </html>
        """,
        url="https://snkrdunk.com/login",
    )

    assessment = classify_page_state("snkrdunk", page, page_type="detail")

    assert assessment.state == "login_required"
    assert assessment.allow_healing is False


def test_classify_snkrdunk_next_data_detail_allows_healing():
    next_data = json.dumps(
        {
            "props": {
                "pageProps": {
                    "item": {
                        "name": "Jordan Static",
                        "price": 22000,
                    }
                }
            }
        }
    )
    page = HtmlPageAdapter(
        f"""
        <html>
          <head><title>Jordan Static | スニダン</title></head>
          <body>
            <script id="__NEXT_DATA__" type="application/json">{next_data}</script>
          </body>
        </html>
        """,
        url="https://snkrdunk.com/products/test-1",
    )

    assessment = classify_page_state("snkrdunk", page, page_type="detail")

    assert assessment.state == "healthy"
    assert assessment.allow_healing is True
