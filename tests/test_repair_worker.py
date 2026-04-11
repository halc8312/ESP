import json

import selector_config
from database import SessionLocal
from models import SelectorActiveRuleSet, SelectorRepairCandidate
from services.html_page_adapter import HtmlPageAdapter
from services.repair_store import record_repair_candidate, reset_repair_store_cache
from services.repair_worker import (
    preview_pending_repair_candidates,
    process_pending_repair_candidates,
    validate_repair_candidate,
)


def _mercari_canary_page(*, title_text: str = "Mercari Product Title", selector_id: str = "healed-title"):
    return HtmlPageAdapter(
        f"""
        <html>
          <head>
            <title>Mercari Item</title>
            <meta name="product:price:amount" content="12000">
          </head>
          <body>
            <main>
              <h1 id="{selector_id}">{title_text}</h1>
              <button>購入手続きへ</button>
            </main>
          </body>
        </html>
        """,
        url="https://jp.mercari.com/item/m123456789",
    )


def _snkrdunk_canary_page(*, title_text: str = "Jordan Static"):
    next_data = json.dumps(
        {
            "props": {
                "pageProps": {
                    "item": {
                        "name": title_text,
                        "price": 22000,
                    }
                }
            }
        }
    )
    return HtmlPageAdapter(
        f"""
        <html>
          <head><title>{title_text} | スニダン</title></head>
          <body>
            <h1 class="product-name-en">{title_text}</h1>
            <script id="__NEXT_DATA__" type="application/json">{next_data}</script>
          </body>
        </html>
        """,
        url="https://snkrdunk.com/products/test-1",
    )


def test_validate_repair_candidate_handles_fetch_failures_without_crashing():
    candidate = {
        "id": 1,
        "site": "mercari",
        "page_type": "detail",
        "field": "title",
        "parser": "scrapling",
        "proposed_selector": "#healed-title",
        "score": 95,
    }

    def failing_fetcher(site: str, url: str):
        raise RuntimeError(f"boom:{url}")

    result = validate_repair_candidate(
        candidate,
        page_fetcher=failing_fetcher,
        canary_urls=[
            "https://jp.mercari.com/item/m111",
            "https://jp.mercari.com/item/m222",
        ],
    )

    assert result["ok"] is False
    assert result["reason"] == "canary_validation_failed"
    assert result["success_count"] == 0
    assert len(result["results"]) == 2
    assert result["results"][0]["reason"] == "fetch_or_classification_failed"


def test_validate_repair_candidate_accepts_snkrdunk_canaries():
    candidate = {
        "id": 2,
        "site": "snkrdunk",
        "page_type": "detail",
        "field": "title",
        "parser": "scrapling",
        "proposed_selector": ".product-name-en",
        "score": 91,
    }
    canary_urls = [
        "https://snkrdunk.com/products/test-1",
        "https://snkrdunk.com/products/test-2",
    ]

    result = validate_repair_candidate(
        candidate,
        page_fetcher=lambda site, url: _snkrdunk_canary_page(),
        canary_urls=canary_urls,
    )

    assert result["ok"] is True
    assert result["reason"] == "validated"
    assert result["success_count"] == 2


def test_process_pending_repair_candidates_promotes_validated_candidate(app, monkeypatch):
    reset_repair_store_cache()
    monkeypatch.setattr(
        selector_config,
        "_selectors_cache",
        {"mercari": {"detail": {"title": [".legacy-title", "h1.legacy"]}}},
    )

    candidate_id = record_repair_candidate(
        site="mercari",
        page_type="detail",
        field="title",
        parser="scrapling",
        proposed_selector="#healed-title",
        source_selector=".legacy-title",
        score=96,
        details={"page_url": "https://jp.mercari.com/item/m123456789"},
    )
    monkeypatch.setattr(
        "services.repair_worker.load_repair_canary_urls",
        lambda site, page_type: [
            "https://jp.mercari.com/item/m111",
            "https://jp.mercari.com/item/m222",
        ],
    )

    pages = {
        "https://jp.mercari.com/item/m111": _mercari_canary_page(),
        "https://jp.mercari.com/item/m222": _mercari_canary_page(),
    }

    summary = process_pending_repair_candidates(
        limit=5,
        page_fetcher=lambda site, url: pages[url],
    )

    assert summary["promoted"] == 1
    assert summary["rejected"] == 0

    session = SessionLocal()
    try:
        candidate = session.get(SelectorRepairCandidate, candidate_id)
        active_rule = (
            session.query(SelectorActiveRuleSet)
            .filter_by(site="mercari", page_type="detail", field="title", is_active=True)
            .one()
        )

        assert candidate.status == "promoted"
        assert active_rule.version == 1
        assert json.loads(active_rule.selectors_payload) == [
            "#healed-title",
            ".legacy-title",
            "h1.legacy",
        ]
        assert active_rule.source_candidate_id == candidate_id
    finally:
        session.close()


def test_process_pending_repair_candidates_rejects_failed_canary(app, monkeypatch):
    reset_repair_store_cache()
    monkeypatch.setattr(
        selector_config,
        "_selectors_cache",
        {"mercari": {"detail": {"title": [".legacy-title", "h1.legacy"]}}},
    )

    candidate_id = record_repair_candidate(
        site="mercari",
        page_type="detail",
        field="title",
        parser="scrapling",
        proposed_selector="#healed-title",
        source_selector=".legacy-title",
        score=96,
    )
    monkeypatch.setattr(
        "services.repair_worker.load_repair_canary_urls",
        lambda site, page_type: [
            "https://jp.mercari.com/item/m111",
            "https://jp.mercari.com/item/m222",
        ],
    )

    pages = {
        "https://jp.mercari.com/item/m111": _mercari_canary_page(),
        "https://jp.mercari.com/item/m222": HtmlPageAdapter(
            """
            <html>
              <head><title>Mercari Login</title></head>
              <body>
                <form action="/login">
                  <label>ログイン</label>
                  <input type="password" name="password">
                </form>
              </body>
            </html>
            """,
            url="https://jp.mercari.com/login",
        ),
    }

    summary = process_pending_repair_candidates(
        limit=5,
        page_fetcher=lambda site, url: pages[url],
    )

    assert summary["promoted"] == 0
    assert summary["rejected"] == 1

    session = SessionLocal()
    try:
        candidate = session.get(SelectorRepairCandidate, candidate_id)
        active_rules = session.query(SelectorActiveRuleSet).all()

        assert candidate.status == "rejected"
        assert json.loads(candidate.details_payload)["validation"]["reason"] == "canary_validation_failed"
        assert active_rules == []
    finally:
        session.close()


def test_process_pending_repair_candidates_skips_when_canaries_are_missing(app, monkeypatch):
    reset_repair_store_cache()
    candidate_id = record_repair_candidate(
        site="mercari",
        page_type="detail",
        field="title",
        parser="scrapling",
        proposed_selector="#healed-title",
        source_selector=".legacy-title",
        score=96,
    )
    monkeypatch.setattr("services.repair_worker.load_repair_canary_urls", lambda site, page_type: [])

    summary = process_pending_repair_candidates(limit=5, page_fetcher=lambda site, url: _mercari_canary_page())

    assert summary["promoted"] == 0
    assert summary["rejected"] == 0
    assert summary["skipped"] == 1

    session = SessionLocal()
    try:
        candidate = session.get(SelectorRepairCandidate, candidate_id)
        assert candidate.status == "pending"
    finally:
        session.close()


def test_preview_pending_repair_candidates_reports_would_promote_without_mutation(app, monkeypatch):
    reset_repair_store_cache()
    monkeypatch.setattr(
        selector_config,
        "_selectors_cache",
        {"mercari": {"detail": {"title": [".legacy-title", "h1.legacy"]}}},
    )

    candidate_id = record_repair_candidate(
        site="mercari",
        page_type="detail",
        field="title",
        parser="scrapling",
        proposed_selector="#healed-title",
        source_selector=".legacy-title",
        score=96,
    )
    monkeypatch.setattr(
        "services.repair_worker.load_repair_canary_urls",
        lambda site, page_type: [
            "https://jp.mercari.com/item/m111",
            "https://jp.mercari.com/item/m222",
        ],
    )

    summary = preview_pending_repair_candidates(
        candidate_id=candidate_id,
        page_fetcher=lambda site, url: _mercari_canary_page(),
    )

    assert summary["inspected"] == 1
    assert summary["would_promote"] == 1
    assert summary["results"][0]["status"] == "would_promote"

    session = SessionLocal()
    try:
        candidate = session.get(SelectorRepairCandidate, candidate_id)
        active_rules = session.query(SelectorActiveRuleSet).all()

        assert candidate.status == "pending"
        assert active_rules == []
    finally:
        session.close()
