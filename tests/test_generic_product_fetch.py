"""
Generic product extraction from unsupported sites.

The supported marketplaces sit behind an exact host allowlist, which rules out
server-side request forgery by construction. Accepting arbitrary URLs gives
that up, so the URL validation is tested at least as carefully as the parsing.
"""
import ipaddress
import socket

import pytest

from services import generic_product_fetch
from services.generic_product_fetch import (
    extract_product_fields,
    fetch_generic_product,
    validate_generic_product_url,
)
from services.scrape_safety import UnsafeScrapeUrlError


def _resolve_to(monkeypatch, address):
    """Pin DNS resolution so the tests never touch the network."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        family = (
            socket.AF_INET6
            if ipaddress.ip_address(address).version == 6
            else socket.AF_INET
        )
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(generic_product_fetch.socket, "getaddrinfo", fake_getaddrinfo)


class TestUrlValidation:
    def test_a_public_https_url_is_accepted(self, monkeypatch):
        _resolve_to(monkeypatch, "93.184.216.34")

        assert validate_generic_product_url("https://shop.example.com/item/1")

    def test_plain_http_is_rejected(self, monkeypatch):
        _resolve_to(monkeypatch, "93.184.216.34")

        with pytest.raises(UnsafeScrapeUrlError):
            validate_generic_product_url("http://shop.example.com/item/1")

    def test_a_non_standard_port_is_rejected(self, monkeypatch):
        _resolve_to(monkeypatch, "93.184.216.34")

        with pytest.raises(UnsafeScrapeUrlError):
            validate_generic_product_url("https://shop.example.com:8443/item/1")

    def test_embedded_credentials_are_rejected(self, monkeypatch):
        _resolve_to(monkeypatch, "93.184.216.34")

        with pytest.raises(UnsafeScrapeUrlError):
            validate_generic_product_url("https://user:pass@shop.example.com/item/1")

    def test_a_bare_ip_is_rejected(self):
        with pytest.raises(UnsafeScrapeUrlError):
            validate_generic_product_url("https://93.184.216.34/item/1")

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",       # loopback
            "10.1.2.3",        # private
            "192.168.0.5",     # private
            "172.16.9.9",      # private
            "169.254.169.254",  # cloud metadata
            "::1",             # IPv6 loopback
            "fd00::1",         # IPv6 unique local
        ],
    )
    def test_a_hostname_pointing_inside_the_network_is_rejected(self, monkeypatch, address):
        _resolve_to(monkeypatch, address)

        with pytest.raises(UnsafeScrapeUrlError):
            validate_generic_product_url("https://internal.example.com/item/1")

    def test_an_unresolvable_hostname_is_rejected(self, monkeypatch):
        def explode(*args, **kwargs):
            raise socket.gaierror("nope")

        monkeypatch.setattr(generic_product_fetch.socket, "getaddrinfo", explode)

        with pytest.raises(UnsafeScrapeUrlError):
            validate_generic_product_url("https://nowhere.example.com/item/1")

    def test_an_empty_url_is_rejected(self):
        with pytest.raises(UnsafeScrapeUrlError):
            validate_generic_product_url("")


JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "ポケモンカード ピカチュウ UR",
  "description": "美品です",
  "image": ["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"],
  "offers": {
    "@type": "Offer",
    "price": "4800",
    "priceCurrency": "JPY",
    "availability": "https://schema.org/InStock"
  }
}
</script>
</head><body></body></html>
"""

OPEN_GRAPH_PAGE = """
<html><head>
<meta property="og:title" content="リザードンV SAR">
<meta property="og:image" content="/images/charizard.jpg">
<meta property="og:description" content="未開封">
<meta property="product:price:amount" content="12,300">
<meta property="product:price:currency" content="JPY">
</head><body></body></html>
"""


class TestExtraction:
    def test_json_ld_gives_every_field(self):
        result = extract_product_fields(JSON_LD_PAGE, "https://shop.example.com/item/1")

        assert result["title"] == "ポケモンカード ピカチュウ UR"
        assert result["price"] == 4800
        assert result["description"] == "美品です"
        assert result["image_urls"] == [
            "https://cdn.example.com/1.jpg",
            "https://cdn.example.com/2.jpg",
        ]
        assert result["availability"] == "InStock"
        assert result["missing_fields"] == []

    def test_open_graph_is_used_when_there_is_no_json_ld(self):
        result = extract_product_fields(OPEN_GRAPH_PAGE, "https://shop.example.com/item/2")

        assert result["title"] == "リザードンV SAR"
        assert result["price"] == 12300
        # A site-relative image is resolved against the page it came from.
        assert result["image_urls"] == ["https://shop.example.com/images/charizard.jpg"]

    def test_what_is_missing_is_reported(self):
        result = extract_product_fields(
            '<html><head><title>名前だけの店</title></head><body></body></html>',
            "https://shop.example.com/item/3",
        )

        assert result["title"] == "名前だけの店"
        assert "price" in result["missing_fields"]
        assert "image_urls" in result["missing_fields"]
        assert "title" in result["found_fields"]

    def test_a_foreign_currency_price_is_left_out_rather_than_mis_filled(self):
        page = """
        <html><head>
        <meta property="og:title" content="Foreign Item">
        <meta property="product:price:amount" content="49.99">
        <meta property="product:price:currency" content="USD">
        </head><body></body></html>
        """

        result = extract_product_fields(page, "https://shop.example.com/item/4")

        assert result["price"] is None
        assert result["currency_warning"] is True
        assert "price" in result["missing_fields"]

    def test_full_width_digits_and_yen_marks_are_understood(self):
        page = """
        <html><head>
        <meta property="og:title" content="全角の店">
        <meta property="product:price:amount" content="￥４，８００">
        </head><body></body></html>
        """

        result = extract_product_fields(page, "https://shop.example.com/item/5")

        assert result["price"] == 4800

    def test_broken_json_ld_does_not_stop_the_open_graph_fallback(self):
        page = """
        <html><head>
        <script type="application/ld+json">{ this is not json </script>
        <meta property="og:title" content="壊れたJSONの店">
        </head><body></body></html>
        """

        result = extract_product_fields(page, "https://shop.example.com/item/6")

        assert result["title"] == "壊れたJSONの店"

    def test_a_product_nested_in_a_graph_is_found(self):
        page = """
        <html><head>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[
          {"@type":"BreadcrumbList"},
          {"@type":"Product","name":"入れ子の商品","offers":{"@type":"Offer","price":900,"priceCurrency":"JPY"}}
        ]}
        </script>
        </head><body></body></html>
        """

        result = extract_product_fields(page, "https://shop.example.com/item/7")

        assert result["title"] == "入れ子の商品"
        assert result["price"] == 900


class TestFetchOutcome:
    def test_a_page_without_a_title_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(
            generic_product_fetch,
            "_fetch_html",
            lambda url: ("<html><head></head><body></body></html>", url),
        )

        outcome = fetch_generic_product("https://shop.example.com/item/1")

        assert outcome["status"] == "failed"
        assert "手動追加" in outcome["error"]

    def test_an_unsafe_url_fails_without_raising(self, monkeypatch):
        _resolve_to(monkeypatch, "127.0.0.1")

        outcome = fetch_generic_product("https://internal.example.com/item/1")

        assert outcome["status"] == "failed"
        assert outcome["product"] is None

    def test_a_readable_page_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            generic_product_fetch,
            "_fetch_html",
            lambda url: (JSON_LD_PAGE, url),
        )

        outcome = fetch_generic_product("https://shop.example.com/item/1")

        assert outcome["status"] == "ok"
        assert outcome["product"]["title"] == "ポケモンカード ピカチュウ UR"
