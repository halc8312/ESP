"""Admin-only health evidence must not become a new sourcing-data surface."""
from datetime import datetime, timedelta

import pytest

from models import User


def _login(client, db_session, role="admin"):
    username = f"health_{role}"
    user = User(username=username, role=role)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})


def _observation(**changes):
    row = {
        "site": "recordcity",
        "route": "search",
        "status": "healthy",
        "last_observed_at": "2026-09-04T08:20:00Z",
        "last_success_at": "2026-09-04T08:20:00+00:00",
        "last_failure_at": None,
        "consecutive_failures": 0,
        "reason": None,
        "latest_delivery_status": "delivered",
    }
    row.update(changes)
    return row


@pytest.fixture
def health_rows(monkeypatch):
    rows = [_observation()]
    monkeypatch.setattr("routes.admin._load_scrape_health_rows", lambda: rows)
    return rows


def test_signed_out_health_page_redirects_before_reading_monitor_store(client, monkeypatch):
    def forbidden_read():
        raise AssertionError("must not read monitoring data before authorization")

    monkeypatch.setattr("routes.admin._load_scrape_health_rows", forbidden_read)
    response = client.get("/admin/scrape-health")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_students_cannot_open_health_page(client, db_session, monkeypatch):
    _login(client, db_session, role="student")

    def forbidden_read():
        raise AssertionError("must not read monitoring data for a student")

    monkeypatch.setattr("routes.admin._load_scrape_health_rows", forbidden_read)
    assert client.get("/admin/scrape-health").status_code == 404
    assert "/admin/scrape-health" not in client.get("/").get_data(as_text=True)


def test_admin_page_explains_passive_scope_and_webhook_acceptance(client, db_session, health_rows):
    _login(client, db_session)
    response = client.get("/admin/scrape-health")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert "レコードシティ" in html
    assert "検索から抽出" in html
    assert "直近の観測は成功" in html
    assert "2026/09/04 17:20" in html
    assert "未観測や取得0件は成功と数えず" in html
    assert "全サイト・全経路の定期テストが実行済み" in html
    assert "レコードシティの登録商品巡回は現在未実装" in html
    assert "未実装・未観測も含む24経路すべてに成功証拠を要求します" in html
    assert "人が通知を受信・確認したことは保証しません" in html
    assert "/admin/scrape-health" in client.get("/admin").get_data(as_text=True)


def test_admin_page_renders_all_site_route_rows_without_collapsing_missing_evidence(
    client, db_session, health_rows
):
    _login(client, db_session)
    sites = ("mercari", "yahoo", "rakuma", "surugaya", "offmall", "yahuoku", "snkrdunk", "recordcity")
    health_rows[:] = [
        _observation(site=site, route=route, status="unobserved", last_success_at=None)
        for site in sites
        for route in ("search", "detail", "patrol")
    ]

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert html.count('<th scope="row">') == 24
    assert html.count("成功未確認") == 24
    assert html.count("<td>商品URL直接</td>") == 8
    assert html.count("<td>登録商品の巡回</td>") == 8


def test_failures_staleness_and_missing_monitor_data_are_not_rendered_as_healthy(
    client, db_session, health_rows
):
    _login(client, db_session)
    health_rows[:] = [
        _observation(status="failed", consecutive_failures=2, latest_delivery_status="failed"),
        _observation(route="detail", status="stale", latest_delivery_status="unconfigured"),
        _observation(route="patrol", status="monitoring_unavailable", last_success_at=None),
    ]

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert "連続失敗・未復旧" in html
    assert "観測が古い" in html
    assert "監視データを読めません" in html
    assert "通知先未設定" in html
    assert "送信失敗" in html
    assert "直近の観測は成功" not in html


def test_unexpected_fields_and_untrusted_labels_do_not_leak_or_execute(
    client, db_session, health_rows
):
    _login(client, db_session)
    attack = '<script>alert("health")</script>'
    health_rows[:] = [_observation(
        site=attack, route=attack, status=attack, reason=attack,
        latest_delivery_status=attack, consecutive_failures=attack,
        source_url="https://internal-sourcing.example/private",
        request_payload={"title": "PRIVATE PRODUCT TITLE"},
        webhook_url="https://private-webhook.example/secret-token",
        last_observed_at=attack,
    )]

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert attack not in html
    assert "&lt;script&gt;" not in html  # Untrusted labels are not echoed, even escaped.
    assert "未登録サイト" in html
    assert "状態不明" in html
    assert "internal-sourcing.example" not in html
    assert "PRIVATE PRODUCT TITLE" not in html
    assert "private-webhook.example" not in html
    assert "secret-token" not in html


def test_empty_health_result_does_not_imply_normal(client, db_session, health_rows):
    _login(client, db_session)
    health_rows.clear()

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert "監視データを確認できません。正常とは判定できません。" in html


@pytest.mark.parametrize("value", [None, "invalid", {}, 123])
def test_invalid_health_timestamps_are_not_echoed(value):
    from routes.admin import _scrape_health_datetime

    assert _scrape_health_datetime(value) is None


def test_health_datetime_also_accepts_existing_datetime_values():
    from routes.admin import _scrape_health_datetime

    value = datetime(2026, 9, 4, 8, 20)
    assert _scrape_health_datetime(value) is value


def test_admin_reads_all_24_unobserved_routes_from_real_database(client, db_session):
    _login(client, db_session)

    response = client.get("/admin/scrape-health")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert html.count('<th scope="row">') == 24
    assert html.count("成功未確認") == 24
    assert "直近の観測は成功" not in html
    assert "監視データを読めません" not in html


def test_admin_keeps_unresolved_incident_visible_when_real_observation_becomes_stale(
    client, db_session, monkeypatch
):
    from services import scrape_health as health

    _login(client, db_session)
    observed_at = datetime(2026, 9, 4, 8, 20)
    monkeypatch.setattr(health, "utc_now", lambda: observed_at)
    for _ in range(2):
        assert health.record_scrape_observation(
            site="recordcity", route="search", outcome="failure", reason="captcha"
        )
    monkeypatch.setattr(health, "utc_now", lambda: observed_at + timedelta(days=2))

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert "観測が古い" in html
    assert "<strong>未復旧の障害あり</strong>" in html
    assert "CAPTCHA要求" in html
    assert "2026/09/04 17:20" in html
    assert "送信待ち" in html


def test_admin_retains_single_failure_evidence_when_stale(client, db_session, monkeypatch):
    from services import scrape_health as health

    _login(client, db_session)
    observed_at = datetime(2026, 9, 4, 8, 20)
    monkeypatch.setattr(health, "utc_now", lambda: observed_at)
    assert health.record_scrape_observation(
        site="mercari", route="detail", outcome="failure", reason="timeout"
    )
    monkeypatch.setattr(health, "utc_now", lambda: observed_at + timedelta(days=2))

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert "<strong>復旧未確認の失敗あり</strong>" in html
    assert "<strong>未復旧の障害あり</strong>" not in html


def test_admin_real_store_read_error_is_explicit_without_exception_text(
    client, db_session, monkeypatch
):
    from services import scrape_health as health

    _login(client, db_session)

    def unavailable():
        raise RuntimeError("private-database-password")

    monkeypatch.setattr(health, "create_isolated_session", unavailable)
    response = client.get("/admin/scrape-health")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert html.count("監視データを読めません") == 24
    assert "直近の観測は成功" not in html
    assert "private-database-password" not in html


def test_admin_shows_unconfigured_alerts_even_without_any_delivery_history(client, db_session):
    _login(client, db_session)

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert "通知先は未設定（この画面のプロセス）" in html
    assert "通知履歴なし" in html
    assert "通知を送るworker側の設定" in html


@pytest.mark.parametrize("category", ["SCRAPE", "SELECTOR", "OPERATIONAL"])
def test_admin_config_presence_includes_fallbacks_without_sending_or_revealing_url(
    client, db_session, monkeypatch, category
):
    from services.alerts import get_alert_dispatcher

    _login(client, db_session)
    secret_url = "https://alerts.example.test/private-token-do-not-expose"
    monkeypatch.setenv(f"{category}_ALERT_WEBHOOK_URL", secret_url)

    def no_send(**kwargs):
        raise AssertionError("Opening the health page must not send notifications")

    monkeypatch.setattr(get_alert_dispatcher(), "notify_scrape_issue_result", no_send)

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert "通知先の設定あり（この画面のプロセス）" in html
    assert secret_url not in html
    assert "private-token-do-not-expose" not in html


def test_unknown_config_diagnostic_does_not_mask_real_database_observations(
    client, db_session, monkeypatch
):
    from services import scrape_health as health

    _login(client, db_session)
    assert health.record_scrape_observation(
        site="recordcity", route="search", outcome="success", success_count=1
    )

    def unavailable():
        raise RuntimeError("private-config-error")

    monkeypatch.setattr(health, "get_alert_dispatcher", unavailable)

    html = client.get("/admin/scrape-health").get_data(as_text=True)

    assert "通知設定を確認できません" in html
    assert "直近の観測は成功" in html
    assert "監視データを読めません" not in html
    assert "private-config-error" not in html
