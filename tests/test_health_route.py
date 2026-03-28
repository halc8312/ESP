def test_healthz_returns_runtime_snapshot(client):
    response = client.get("/healthz")

    assert response.status_code == 200

    payload = response.get_json()
    assert payload == {
        "status": "ok",
        "runtime_role": "test",
        "queue_backend": "inmemory",
        "scheduler_enabled": False,
    }
