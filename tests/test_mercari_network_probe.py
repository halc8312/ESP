import json
from pathlib import Path

from services.mercari_network_probe import (
    collect_status_records,
    summarize_html_signals,
    summarize_payload,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mercari"


def test_collect_status_records_finds_nested_status_nodes():
    payload = {
        "data": {
            "search": {
                "items": [
                    {"id": "m1", "name": "Trading Item", "status": "ITEM_STATUS_TRADING"},
                    {"itemId": "m2", "title": "Live Item", "saleStatus": "STATUS_ON_SALE", "soldOut": False},
                ]
            }
        }
    }

    records = collect_status_records(payload)

    assert any(record.get("id") == "m1" and record["fields"].get("status") == "ITEM_STATUS_TRADING" for record in records)
    assert any(record.get("itemId") == "m2" and record["fields"].get("saleStatus") == "STATUS_ON_SALE" for record in records)


def test_summarize_html_signals_detects_next_flight_without_next_data():
    html = """
    <html>
      <body>
        <script>self.__next_f.push([1, "payload"]);</script>
        <button>購入手続きへ</button>
      </body>
    </html>
    """

    signals = summarize_html_signals(html)

    assert signals["has_next_flight"] is True
    assert signals["has_next_data"] is False
    assert "購入手続きへ" in signals["matched_tokens"]


def test_summarize_payload_uses_existing_parser_for_detail_fixture():
    payload = json.loads((FIXTURE_DIR / "network_payload_item.json").read_text(encoding="utf-8"))

    summary = summarize_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert summary["status_value_counts"]["on_sale"] == 1
    assert summary["parsed_item"]["title"] == "Payload Sneakers"
    assert summary["parsed_item"]["price"] == 2980
    assert summary["parsed_item"]["status"] == "on_sale"
    assert summary["parsed_meta"]["field_sources"]["price"] == "payload"
