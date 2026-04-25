import json
import uuid
from pathlib import Path

import database
import selector_config
from database import SessionLocal
from models import SelectorActiveRuleSet, SelectorRepairCandidate
from services.repair_store import (
    get_pending_repair_candidate,
    get_repair_queue_snapshot,
    inspect_repair_store_state,
    load_active_selectors,
    promote_repair_candidate,
    record_repair_candidate,
    reset_repair_store_cache,
)


def test_load_active_selectors_returns_none_when_store_tables_are_missing(monkeypatch):
    tmp_root = Path(__file__).resolve().parent / ".tmp"
    tmp_root.mkdir(exist_ok=True)
    database_path = tmp_root / f"repair_store_missing_{uuid.uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"

    previous_engine = database.engine
    test_engine = database.create_app_engine(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    SessionLocal.remove()
    database.engine = test_engine
    SessionLocal.configure(bind=test_engine)
    reset_repair_store_cache()

    try:
        assert load_active_selectors("mercari", "detail", "title") is None
        assert record_repair_candidate(
            site="mercari",
            page_type="detail",
            field="title",
            parser="scrapling",
            proposed_selector="#healed-title",
            source_selector=".legacy-title",
            score=88,
        ) is None
    finally:
        SessionLocal.remove()
        test_engine.dispose()
        database.engine = previous_engine
        SessionLocal.configure(bind=database.engine)
        reset_repair_store_cache()
        if database_path.exists():
            database_path.unlink()


def test_inspect_repair_store_state_reports_missing_tables(monkeypatch):
    tmp_root = Path(__file__).resolve().parent / ".tmp"
    tmp_root.mkdir(exist_ok=True)
    database_path = tmp_root / f"repair_store_state_{uuid.uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"

    previous_engine = database.engine
    test_engine = database.create_app_engine(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    SessionLocal.remove()
    database.engine = test_engine
    SessionLocal.configure(bind=test_engine)
    reset_repair_store_cache()

    try:
        snapshot = inspect_repair_store_state()

        assert snapshot["ready"] is False
        assert snapshot["blockers"] == ["repair_store_tables_missing"]
        assert "selector_repair_candidates" in snapshot["missing_tables"]
        assert "selector_active_rule_sets" in snapshot["missing_tables"]
    finally:
        SessionLocal.remove()
        test_engine.dispose()
        database.engine = previous_engine
        SessionLocal.configure(bind=database.engine)
        reset_repair_store_cache()
        if database_path.exists():
            database_path.unlink()


def test_record_repair_candidate_persists_candidate(app):
    reset_repair_store_cache()

    candidate_id = record_repair_candidate(
        site="mercari",
        page_type="detail",
        field="title",
        parser="scrapling",
        proposed_selector="#healed-title",
        source_selector=".legacy-title",
        score=92.4,
        details={"persisted_to_json": True},
    )

    session = SessionLocal()
    try:
        candidate = session.get(SelectorRepairCandidate, candidate_id)
        assert candidate is not None
        assert candidate.site == "mercari"
        assert candidate.page_type == "detail"
        assert candidate.field == "title"
        assert candidate.proposed_selector == "#healed-title"
        assert candidate.score == 92
        assert json.loads(candidate.details_payload)["persisted_to_json"] is True
    finally:
        session.close()


def test_get_pending_repair_candidate_returns_serialized_candidate(app):
    reset_repair_store_cache()

    candidate_id = record_repair_candidate(
        site="mercari",
        page_type="detail",
        field="title",
        parser="scrapling",
        proposed_selector="#healed-title",
        source_selector=".legacy-title",
        score=92,
        details={"persisted_to_json": True},
    )

    candidate = get_pending_repair_candidate(candidate_id)

    assert candidate is not None
    assert candidate["id"] == candidate_id
    assert candidate["site"] == "mercari"
    assert candidate["details"]["persisted_to_json"] is True


def test_get_repair_queue_snapshot_reports_pending_candidates(app):
    reset_repair_store_cache()

    candidate_id = record_repair_candidate(
        site="mercari",
        page_type="detail",
        field="title",
        parser="scrapling",
        proposed_selector="#healed-title",
        source_selector=".legacy-title",
        score=92,
    )

    snapshot = get_repair_queue_snapshot()

    assert snapshot["ready"] is True
    assert snapshot["pending_count"] == 1
    assert snapshot["sample_candidate_ids"] == [candidate_id]
    assert snapshot["oldest_pending_created_at"] is not None


def test_selector_config_prefers_db_active_selectors(app, monkeypatch):
    reset_repair_store_cache()
    monkeypatch.setattr(
        selector_config,
        "_selectors_cache",
        {"mercari": {"detail": {"title": [".json-title", "h1.legacy"]}}},
    )

    session = SessionLocal()
    try:
        session.add(
            SelectorActiveRuleSet(
                site="mercari",
                page_type="detail",
                field="title",
                version=1,
                selectors_payload=json.dumps(["#db-title", "h1[data-testid='name']"]),
                is_active=True,
            )
        )
        session.commit()
    finally:
        session.close()

    assert selector_config.get_selectors("mercari", "detail", "title") == [
        "#db-title",
        "h1[data-testid='name']",
    ]


def test_selector_config_falls_back_to_json_when_no_active_rule(app, monkeypatch):
    reset_repair_store_cache()
    monkeypatch.setattr(
        selector_config,
        "_selectors_cache",
        {"mercari": {"detail": {"title": [".json-title", "h1.legacy"]}}},
    )

    assert selector_config.get_selectors("mercari", "detail", "title") == [
        ".json-title",
        "h1.legacy",
    ]


def test_promote_repair_candidate_versions_active_rules(app):
    reset_repair_store_cache()

    session = SessionLocal()
    try:
        candidate = SelectorRepairCandidate(
            site="mercari",
            page_type="detail",
            field="title",
            parser="scrapling",
            proposed_selector="#healed-title",
            source_selector=".legacy-title",
            score=96,
            page_state="healthy",
            status="pending",
        )
        session.add(candidate)
        session.add(
            SelectorActiveRuleSet(
                site="mercari",
                page_type="detail",
                field="title",
                version=1,
                selectors_payload=json.dumps([".legacy-title", "h1.legacy"]),
                is_active=True,
            )
        )
        session.commit()
        candidate_id = int(candidate.id)
    finally:
        session.close()

    result = promote_repair_candidate(
        candidate_id,
        selectors_payload=["#healed-title", ".legacy-title", "h1.legacy"],
        validation_summary={"validation": {"ok": True, "canary_count": 2}},
    )

    assert result["ok"] is True
    assert result["version"] == 2

    session = SessionLocal()
    try:
        candidate = session.get(SelectorRepairCandidate, candidate_id)
        rules = (
            session.query(SelectorActiveRuleSet)
            .filter_by(site="mercari", page_type="detail", field="title")
            .order_by(SelectorActiveRuleSet.version.asc())
            .all()
        )

        assert candidate.status == "promoted"
        assert json.loads(candidate.details_payload)["validation"]["canary_count"] == 2
        assert len(rules) == 2
        assert rules[0].version == 1 and rules[0].is_active is False
        assert rules[1].version == 2 and rules[1].is_active is True
        assert rules[1].source_candidate_id == candidate_id
        assert rules[1].activated_at is not None
    finally:
        session.close()
