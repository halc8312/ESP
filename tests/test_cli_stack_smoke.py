import json
from pathlib import Path
import uuid

import pytest

from cli import (
    _load_stack_smoke_fixture,
    _response_contains_title,
    run_detail_fixture_smoke,
    run_search_fixture_smoke,
)


def test_stack_smoke_cli_prints_json(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_stack_smoke",
        lambda current_app, **kwargs: {
            "queue_name": "stack-smoke-test",
            "blockers": [],
            "ready": True,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["stack-smoke", "--require-backend", "postgresql"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["queue_name"] == "stack-smoke-test"
    assert payload["ready"] is True


def test_stack_smoke_cli_fails_on_blocker(app, monkeypatch):
    monkeypatch.setattr(
        "cli.run_stack_smoke",
        lambda current_app, **kwargs: {
            "queue_name": "stack-smoke-test",
            "blockers": ["redis_connection_failed"],
            "ready": False,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=["stack-smoke"])

    assert result.exit_code == 1


def test_stack_smoke_cli_passes_fixture_options(app, monkeypatch):
    captured = {}

    def fake_run_stack_smoke(current_app, **kwargs):
        captured.update(kwargs)
        return {
            "queue_name": "stack-smoke-test",
            "blockers": [],
            "ready": True,
        }

    monkeypatch.setattr("cli.run_stack_smoke", fake_run_stack_smoke)

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "stack-smoke",
            "--fixture-site",
            "mercari",
            "--fixture-path",
            "mercari_page_dump_live.html",
            "--fixture-target-url",
            "https://jp.mercari.com/item/m71383569733",
        ]
    )

    assert result.exit_code == 0
    assert captured["fixture_site"] == "mercari"
    assert captured["fixture_path"] == "mercari_page_dump_live.html"
    assert captured["fixture_target_url"] == "https://jp.mercari.com/item/m71383569733"


def test_load_stack_smoke_fixture_extracts_active_mercari_live_fixture():
    fixture = _load_stack_smoke_fixture(
        "mercari",
        str(Path("mercari_page_dump_live.html")),
        target_url="https://jp.mercari.com/item/m71383569733",
    )

    assert fixture["site"] == "mercari"
    assert fixture["meta"]["page_type"] == "active_detail"
    assert fixture["item"]["status"] == "on_sale"
    assert fixture["item"]["price"] == 4999
    assert fixture["item"]["title"]
    assert fixture["item"]["image_urls"]


def test_load_stack_smoke_fixture_extracts_active_snkrdunk_fixture():
    fixture = _load_stack_smoke_fixture(
        "snkrdunk",
        str(Path("dump.html")),
        target_url="https://snkrdunk.com/products/nike-air-max-95-og-big-bubble-neon-yellow-2025-2026",
    )

    assert fixture["site"] == "snkrdunk"
    assert fixture["meta"]["page_type"] == "active_detail"
    assert fixture["item"]["status"] == "on_sale"
    assert fixture["item"]["price"] == 29044
    assert fixture["item"]["title"].startswith("Nike Air Max 95 OG Big Bubble")
    assert fixture["item"]["image_urls"]


def test_load_stack_smoke_fixture_rejects_deleted_mercari_fixture():
    with pytest.raises(ValueError, match="active or sold detail page"):
        _load_stack_smoke_fixture(
            "mercari",
            str(Path("mercari_page_dump.html")),
            target_url="https://jp.mercari.com/item/m71383569733",
        )


def test_response_contains_title_handles_html_entities():
    body = 'Nike Air Max 95 OG Big Bubble &#34;Neon Yellow&#34; (2025/2026)'
    assert _response_contains_title(body, 'Nike Air Max 95 OG Big Bubble "Neon Yellow" (2025/2026)')


def test_run_detail_fixture_smoke_reports_active_mercari_fixture():
    snapshot = run_detail_fixture_smoke(
        "mercari",
        "mercari_page_dump_live.html",
        target_url="https://jp.mercari.com/item/m71383569733",
    )

    assert snapshot["ready"] is True
    assert snapshot["status"] == "on_sale"
    assert snapshot["page_type"] == "active_detail"
    assert snapshot["price"] == 4999
    assert snapshot["image_count"] >= 1
    assert snapshot["blockers"] == []


def test_run_detail_fixture_smoke_reports_deleted_mercari_fixture_without_failing():
    snapshot = run_detail_fixture_smoke(
        "mercari",
        "mercari_page_dump.html",
        target_url="https://jp.mercari.com/item/m71383569733",
    )

    assert snapshot["ready"] is True
    assert snapshot["status"] == "deleted"
    assert "status_deleted" in snapshot["warnings"]
    assert snapshot["blockers"] == []


def test_detail_fixture_smoke_cli_strict_fails_on_deleted_fixture(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "detail-fixture-smoke",
            "--site",
            "mercari",
            "--fixture-path",
            "mercari_page_dump.html",
            "--target-url",
            "https://jp.mercari.com/item/m71383569733",
            "--strict",
        ]
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "deleted"
    assert "title_missing" in payload["blockers"]


def test_run_search_fixture_smoke_reports_rendered_results():
    fixture_dir = Path(__file__).resolve().parent / ".tmp"
    fixture_dir.mkdir(exist_ok=True)
    fixture_path = fixture_dir / f"mercari_search_rendered_{uuid.uuid4().hex}.html"
    fixture_path.write_text(
        """
        <html>
          <head>
            <title>camera の検索結果 - メルカリ</title>
            <link rel="canonical" href="https://jp.mercari.com/search?keyword=camera">
          </head>
          <body>
            <h1>camera の検索結果</h1>
            <ul>
              <li data-testid="item-cell">
                <a href="/item/m12345678901">item 1</a>
              </li>
              <li data-testid="item-cell">
                <a href="https://jp.mercari.com/item/m12345678902">item 2</a>
              </li>
            </ul>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    try:
        snapshot = run_search_fixture_smoke(
            "mercari",
            str(fixture_path),
            target_url="https://jp.mercari.com/search?keyword=camera",
        )

        assert snapshot["ready"] is True
        assert snapshot["page_type"] == "search_results"
        assert snapshot["item_count"] == 2
        assert snapshot["blockers"] == []
        assert snapshot["sample_item_urls"][0] == "https://jp.mercari.com/item/m12345678901"
    finally:
        if fixture_path.exists():
            fixture_path.unlink()


def test_run_search_fixture_smoke_reports_skeleton_only_dump():
    snapshot = run_search_fixture_smoke(
        "mercari",
        "search_dump.html",
        target_url="https://jp.mercari.com/search?keyword=sneaker",
    )

    assert snapshot["ready"] is False
    assert snapshot["page_type"] == "search_skeleton"
    assert "item_urls_missing" in snapshot["blockers"]
    assert "search_results_not_rendered" in snapshot["blockers"]


def test_search_fixture_smoke_cli_fails_on_skeleton_dump(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "search-fixture-smoke",
            "--site",
            "mercari",
            "--fixture-path",
            "search_dump.html",
            "--target-url",
            "https://jp.mercari.com/search?keyword=sneaker",
        ]
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["page_type"] == "search_skeleton"
    assert "search_results_not_rendered" in payload["blockers"]
