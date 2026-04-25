def test_force_https_redirects_http_requests(client):
    client.application.config["FORCE_HTTPS"] = True

    response = client.get("/login", base_url="http://example.test")

    assert response.status_code == 301
    assert response.headers["Location"].startswith("https://example.test/login")


def test_hsts_header_added_on_https(client):
    client.application.config["HSTS_ENABLED"] = True
    client.application.config["HSTS_MAX_AGE"] = 123
    client.application.config["HSTS_INCLUDE_SUBDOMAINS"] = True
    client.application.config["HSTS_PRELOAD"] = False

    response = client.get("/login", base_url="https://example.test")

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"] == "max-age=123; includeSubDomains"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
