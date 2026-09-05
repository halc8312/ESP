from datetime import timedelta

import pytest

from jobs import scrape_tasks
from models import Product, User
from services.monitor_service import MonitorService
from services.patrol.base_patrol import PatrolResult
from services.scrape_observation import inspect_scraped_items, record_observation_safely
from time_utils import utc_now


def _item(**overrides):
    return {"title": "Fixture", "url": "https://jp.mercari.com/item/m-fixture", "price": 1000, "status": "on_sale", **overrides}


@pytest.fixture
def task_observations(monkeypatch):
    observations = []
    monkeypatch.setattr(scrape_tasks, "record_observation_safely", lambda **kw: observations.append(kw))
    monkeypatch.setattr(scrape_tasks, "filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr(scrape_tasks, "filter_items_by_price", lambda items, **kwargs: (items, 0))
    return observations


def test_search_observes_pre_filter_results(task_observations, monkeypatch):
    monkeypatch.setattr(scrape_tasks, "scrape_search_result", lambda **kw: [_item()])
    monkeypatch.setattr(scrape_tasks, "filter_excluded_items", lambda items, user_id: ([], len(items)))
    result = scrape_tasks.execute_scrape_job({"site": "mercari", "keyword": "fixture", "persist_to_db": False})
    assert result["items"] == []
    assert task_observations == [dict(site="mercari", route="search", outcome="success", reason=None, success_count=1, error_count=0)]


def test_direct_detail_uses_classified_site_not_request_site(task_observations, monkeypatch):
    monkeypatch.setattr(scrape_tasks, "classify_target_url", lambda url: ("item", "recordcity"))
    monkeypatch.setattr(scrape_tasks.recordcity_db, "scrape_single_item", lambda *args, **kw: [_item()])
    scrape_tasks.execute_scrape_job({"site": "mercari", "target_url": "https://www.recordcity.jp/ja/catalog/fixture", "persist_to_db": False})
    assert task_observations[0]["site"] == "recordcity"
    assert task_observations[0]["route"] == "detail"
    assert task_observations[0]["outcome"] == "success"


def test_search_empty_is_not_success_or_failure(task_observations, monkeypatch):
    monkeypatch.setattr(scrape_tasks, "scrape_search_result", lambda **kw: [])
    scrape_tasks.execute_scrape_job({"keyword": "fixture", "persist_to_db": False})
    assert task_observations[0]["outcome"] == "no_observations"
    assert task_observations[0]["success_count"] == 0
    assert task_observations[0]["error_count"] == 0


@pytest.mark.parametrize("overrides,reason", [
    ({"price": None}, "missing_price"),
    ({"price": 0}, "missing_price"),
    ({"price": float("nan")}, "missing_price"),
    ({"status": "unknown"}, "unknown_status"),
    ({"title": ""}, "invalid_result"),
])
def test_partial_quality_failure_not_hidden_by_valid_item(overrides, reason):
    observed = inspect_scraped_items([_item(), _item(**overrides)])
    assert observed == dict(outcome="failure", reason=reason, success_count=1, error_count=1)


@pytest.mark.parametrize("status", ["sold", "deleted"])
def test_removed_items_do_not_require_current_price(status):
    assert inspect_scraped_items([_item(price=None, status=status)])["outcome"] == "success"


@pytest.mark.parametrize("smoke_error", ["", "fixture error"])
def test_synthetic_smoke_never_changes_live_health(task_observations, smoke_error):
    payload = {"persist_to_db": False, "__smoke_result": {"items": [_item()], "error_msg": smoke_error}}
    if smoke_error:
        with pytest.raises(RuntimeError):
            scrape_tasks.execute_scrape_job(payload)
    else:
        scrape_tasks.execute_scrape_job(payload)
    assert task_observations == []


def test_fetch_error_reason_is_redacted(task_observations, monkeypatch):
    def fail(**kw):
        raise RuntimeError("CAPTCHA https://private.example/?token=secret-value")
    monkeypatch.setattr(scrape_tasks, "scrape_search_result", fail)
    with pytest.raises(RuntimeError):
        scrape_tasks.execute_scrape_job({"persist_to_db": False})
    assert task_observations[0]["reason"] == "captcha"
    assert "secret-value" not in repr(task_observations)


def test_persistence_failure_is_not_a_success(task_observations, monkeypatch):
    monkeypatch.setattr(scrape_tasks, "scrape_search_result", lambda **kw: [_item()])
    def fail(*args, **kw):
        assert kw["raise_on_error"] is True
        raise RuntimeError("private database connection details")
    monkeypatch.setattr(scrape_tasks, "save_scraped_items_to_db", fail)
    with pytest.raises(RuntimeError):
        scrape_tasks.execute_scrape_job({"persist_to_db": True})
    assert task_observations == [dict(site="mercari", route="search", outcome="failure", reason="persistence_error", success_count=0, error_count=1)]


def test_invalid_request_does_not_mark_site_broken(task_observations, monkeypatch):
    monkeypatch.setattr(scrape_tasks, "classify_target_url", lambda url: (_ for _ in ()).throw(ValueError("bad input")))
    with pytest.raises(ValueError):
        scrape_tasks.execute_scrape_job({"target_url": "not a supported URL"})
    assert task_observations == []


def test_monitoring_write_failure_does_not_escape(monkeypatch, caplog):
    def fail(**kw):
        raise RuntimeError("database password secret-value")
    monkeypatch.setattr("services.scrape_health.record_scrape_observation", fail)
    assert record_observation_safely(site="mercari", route="search", outcome="success", success_count=1) is False
    assert "error_type=RuntimeError" in caplog.text
    assert "secret-value" not in caplog.text


def _product(db_session, *, url="https://jp.mercari.com/item/m-passive", **overrides):
    user = User(username="passive-observation-user")
    user.set_password("fixture-password")
    db_session.add(user)
    db_session.flush()
    product = Product(user_id=user.id, site="mercari", source_url=url, last_title="Fixture", last_price=1000,
                      last_status="on_sale", is_listed=True, archived=False, updated_at=utc_now()-timedelta(days=1), **overrides)
    db_session.add(product)
    db_session.commit()
    return product


@pytest.fixture
def patrol_observations(monkeypatch):
    observations = []
    monkeypatch.setattr("services.monitor_service.record_observation_safely", lambda **kw: observations.append(kw))
    return observations


def _patrol(monkeypatch, result):
    class FakePatrol:
        def fetch(self, url):
            if isinstance(result, Exception):
                raise result
            return result
    monkeypatch.setattr(MonitorService, "_patrols", {"mercari": FakePatrol()})


def test_unchanged_committed_patrol_is_success(db_session, monkeypatch, patrol_observations):
    _product(db_session)
    _patrol(monkeypatch, PatrolResult(price=1000, status="active"))
    summary = MonitorService.check_stale_products()
    assert summary["updated_count"] == 0
    assert summary["successful_count"] == 1
    assert summary["error_count"] == 0
    assert patrol_observations[0]["outcome"] == "success"


def test_invalid_patrol_url_is_observed_without_fetch(db_session, monkeypatch, patrol_observations):
    _product(db_session, url="https://jp.mercari.com/search?keyword=fixture")
    _patrol(monkeypatch, AssertionError("must not fetch"))
    summary = MonitorService.check_stale_products()
    assert summary["error_count"] == 1
    assert summary["successful_count"] == 0
    assert patrol_observations[0]["reason"] == "invalid_url"


def test_repricing_error_not_counted_as_committed_update(db_session, monkeypatch, patrol_observations):
    _product(db_session)
    _patrol(monkeypatch, PatrolResult(price=2000, status="active"))
    monkeypatch.setattr("services.monitor_service.product_has_pricing_config", lambda product: True)
    def fail(*args, **kw):
        raise RuntimeError("private connection")
    monkeypatch.setattr("services.monitor_service.update_product_selling_price", fail)
    summary = MonitorService.check_stale_products()
    assert summary["updated_count"] == summary["successful_count"] == 0
    assert summary["error_count"] == 1
    assert patrol_observations[0]["reason"] == "persistence_error"


def test_active_patrol_missing_high_confidence_price_not_healthy(db_session, monkeypatch, patrol_observations):
    _product(db_session)
    _patrol(monkeypatch, PatrolResult(price=None, status="active", confidence="high"))
    summary = MonitorService.check_stale_products()
    assert summary["successful_count"] == 0
    assert summary["error_count"] == 1
    assert patrol_observations[0]["reason"] == "missing_price"


def test_empty_patrol_makes_no_site_success_claim(db_session, patrol_observations):
    summary = MonitorService.check_stale_products()
    assert summary["status"] == "no_products"
    assert summary["successful_count"] == 0
    assert patrol_observations == []
