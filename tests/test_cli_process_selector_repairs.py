import json


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_run_selector_repair_cycle_dry_run_uses_preview(monkeypatch, app):
    captured = {}

    monkeypatch.setattr(
        "cli.inspect_repair_store_state",
        lambda: {
            "ready": True,
            "enabled": True,
            "known_unavailable": False,
            "missing_tables": [],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        "cli.preview_pending_repair_candidates",
        lambda **kwargs: (
            captured.setdefault("preview_kwargs", kwargs),
            {
                "inspected": 1,
                "would_promote": 1,
                "would_reject": 0,
                "would_skip": 0,
                "results": [{"candidate_id": 7, "status": "would_promote"}],
            },
        )[1],
    )
    monkeypatch.setattr(
        "cli.process_pending_repair_candidates",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("apply path should not run")),
    )

    from cli import run_selector_repair_cycle

    snapshot = run_selector_repair_cycle(app, limit=3, candidate_id=7, apply=False)

    assert snapshot["ready"] is True
    assert snapshot["mode"] == "dry_run"
    assert snapshot["candidate_id"] == 7
    assert snapshot["would_promote"] == 1
    assert captured["preview_kwargs"] == {"limit": 3, "candidate_id": 7}


def test_run_selector_repair_cycle_apply_uses_processor(monkeypatch, app):
    captured = {}

    monkeypatch.setattr(
        "cli.inspect_repair_store_state",
        lambda: {
            "ready": True,
            "enabled": True,
            "known_unavailable": False,
            "missing_tables": [],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        "cli.process_pending_repair_candidates",
        lambda **kwargs: (
            captured.setdefault("process_kwargs", kwargs),
            {
                "inspected": 1,
                "promoted": 1,
                "rejected": 0,
                "skipped": 0,
                "results": [{"candidate_id": 7, "status": "promoted"}],
            },
        )[1],
    )

    from cli import run_selector_repair_cycle

    snapshot = run_selector_repair_cycle(app, limit=2, candidate_id=7, apply=True)

    assert snapshot["ready"] is True
    assert snapshot["mode"] == "apply"
    assert snapshot["promoted"] == 1
    assert captured["process_kwargs"] == {"limit": 2, "candidate_id": 7}


def test_run_selector_repair_cycle_blocks_when_candidate_is_missing(monkeypatch, app):
    monkeypatch.setattr(
        "cli.inspect_repair_store_state",
        lambda: {
            "ready": True,
            "enabled": True,
            "known_unavailable": False,
            "missing_tables": [],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        "cli.preview_pending_repair_candidates",
        lambda **kwargs: {
            "inspected": 0,
            "would_promote": 0,
            "would_reject": 0,
            "would_skip": 0,
            "results": [],
        },
    )

    from cli import run_selector_repair_cycle

    snapshot = run_selector_repair_cycle(app, limit=1, candidate_id=99, apply=False)

    assert snapshot["ready"] is False
    assert snapshot["blockers"] == ["candidate_not_found"]


def test_process_selector_repairs_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_selector_repair_cycle",
        lambda current_app, **kwargs: {
            "ready": True,
            "mode": "dry_run",
            "apply": False,
            "limit": kwargs["limit"],
            "candidate_id": kwargs["candidate_id"],
            "blockers": [],
            "warnings": [],
            "inspected": 1,
            "would_promote": 1,
            "would_reject": 0,
            "would_skip": 0,
            "results": [{"candidate_id": 7, "status": "would_promote"}],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["process-selector-repairs", "--limit", "4", "--candidate-id", "7"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["mode"] == "dry_run"
    assert payload["limit"] == 4
    assert payload["candidate_id"] == 7


def test_process_selector_repairs_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_selector_repair_cycle",
        lambda current_app, **kwargs: {
            "ready": False,
            "mode": "dry_run",
            "apply": False,
            "limit": kwargs["limit"],
            "candidate_id": kwargs["candidate_id"],
            "blockers": ["candidate_not_found"],
            "warnings": [],
            "inspected": 0,
            "results": [],
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["process-selector-repairs", "--candidate-id", "999"])

    assert result.exit_code == 1
