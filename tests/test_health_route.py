def test_healthz_returns_runtime_snapshot(client):
    response = client.get("/healthz")

    assert response.status_code == 200

    payload = response.get_json()
    assert payload == {
        "status": "ok",
        "runtime_role": "test",
        "queue_backend": "inmemory",
        "scheduler_enabled": False,
        "scheduler": {
            "enabled": False,
            "started": False,
            "start_attempted": False,
            "running": False,
            "jobs_registered": False,
            "job_ids": [],
            "job_lookup_error": None,
            "lock_backend": None,
            "lock_acquired": None,
            "lock_reason": None,
        },
    }
