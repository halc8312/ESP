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
            "lock_ttl_seconds": None,
            "lock_stale_cleared": False,
            "retry_enabled": False,
            "retry_scheduled": False,
            "retry_attempts": 0,
            "retry_next_at": None,
            "retry_succeeded_at": None,
            "retry_exhausted_at": None,
            "retry_last_error": None,
            "heartbeat_key": "esp:scheduler:heartbeat",
            "heartbeat_error": "disabled",
            "heartbeat": None,
        },
    }
