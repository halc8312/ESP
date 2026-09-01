import json

import pytest

from services import recordcity_probe
from services.scrape_request import InvalidTargetUrl


DETAIL_URL = "https://www.recordcity.jp/catalog/4936480"
PRODUCT_HTML = """<html><body>
<script type="application/ld+json">{"@type":"Product","sku":"4936480"}</script>
</body></html>"""
CHALLENGE_HTML = """<html><script>
window.gokuProps = {};
AwsWafIntegration.fetch('https://example.token.awswaf.com/challenge.js');
</script></html>"""
NORMAL_UA = "Mozilla/5.0 Chrome/145.0.0.0 Safari/537.36"
HEADLESS_UA = "Mozilla/5.0 HeadlessChrome/145.0.0.0 Safari/537.36"


def _attempted(strategy, *, outcome="success"):
    result = recordcity_probe._base_result(strategy)
    result.update(
        {
            "attempted": True,
            "target_status": 200,
            "ready_dom": outcome == "success",
            "product_json_ld": outcome == "success",
            "outcome": outcome,
            "elapsed_ms": 10,
        }
    )
    return result


def _waf_failure(strategy):
    result = _attempted(strategy, outcome="blocked_403")
    result["target_status"] = 403
    result["blocked_marker"] = True
    return result


def _with_browser_signal(row, *, user_agent=NORMAL_UA, headless=False):
    row["user_agent"] = user_agent
    row["headless_user_agent"] = headless
    return row


def test_html_result_distinguishes_challenge_from_product():
    challenge = recordcity_probe._result_from_html(
        "curl-chrome120",
        html=CHALLENGE_HTML,
        kind="detail",
        target_status=202,
        transport_status=None,
        headers={
            "x-amzn-waf-action": "challenge",
            "server": "CloudFront",
            "x-cache": "Error from cloudfront",
            "x-amz-cf-id": "request-id",
        },
        header_source="target",
        token_present=False,
        elapsed_ms=20,
    )
    product = recordcity_probe._result_from_html(
        "zyte",
        html=PRODUCT_HTML,
        kind="detail",
        target_status=200,
        transport_status=200,
        headers={},
        header_source="provider",
        token_present=False,
        elapsed_ms=30,
    )

    assert challenge["outcome"] == "challenge"
    assert challenge["challenge"] is True
    assert challenge["cloudfront_request_ids"] == ["request-id"]
    assert challenge["product_json_ld"] is False
    assert product["outcome"] == "success"
    assert product["product_json_ld"] is True
    assert "Product" not in repr(product)
    assert "4936480" not in repr(product)
    assert "html" not in product
    assert "body" not in product
    assert len(product["body_sha256"]) == 64


def test_expected_detail_sku_ignores_query_string():
    assert recordcity_probe._expected_detail_sku(
        DETAIL_URL + "?campaign=probe",
        "detail",
    ) == "4936480"


def test_probe_validates_url_before_any_strategy(monkeypatch):
    monkeypatch.setattr(
        recordcity_probe,
        "_probe_curl",
        lambda *_args, **_kwargs: pytest.fail("network strategy must not run"),
    )

    with pytest.raises(InvalidTargetUrl):
        recordcity_probe.run_recordcity_probe(
            "https://www.recordcity.jp.evil.example/catalog/4936480",
            strategies=["curl-chrome120"],
            delay_seconds=0,
        )


def test_probe_runs_each_selected_strategy_once_with_one_pause(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(
        recordcity_probe,
        "_probe_curl",
        lambda _url, **kwargs: (
            calls.append(kwargs["strategy"])
            or _attempted(kwargs["strategy"])
        ),
    )
    monkeypatch.setattr(
        recordcity_probe,
        "_probe_browser",
        lambda _url, **kwargs: (
            calls.append(kwargs["strategy"])
            or _attempted(kwargs["strategy"])
        ),
    )

    snapshot = recordcity_probe.run_recordcity_probe(
        DETAIL_URL,
        strategies=["curl-chrome120", "patchright-headful", "curl-chrome120"],
        delay_seconds=4,
        sleep_fn=sleeps.append,
    )

    assert snapshot["strategies"] == ["curl-chrome120", "patchright-headful"]
    assert calls == ["curl-chrome120", "patchright-headful"]
    assert sleeps == [4.0]
    assert len(snapshot["results"]) == 2


def test_browser_detail_requires_product_json_ld(monkeypatch):
    monkeypatch.setattr(
        "services.recordcity_browser_fetch.probe_recordcity_browser_once_sync",
        lambda *_args, **_kwargs: {
            **_attempted("patchright-current"),
            "ready_dom": True,
            "product_json_ld": False,
        },
    )

    row = recordcity_probe._probe_browser(
        DETAIL_URL,
        kind="detail",
        strategy="patchright-current",
        timeout_seconds=5,
    )

    assert row["outcome"] == "target_dom_missing"


def test_external_provider_waf_body_without_metadata_is_ambiguous(monkeypatch):
    from services.recordcity_external_fetch import RecordCityExternalResponse

    monkeypatch.setattr(
        "services.recordcity_external_fetch.fetch_recordcity_external",
        lambda *_args, **_kwargs: RecordCityExternalResponse(
            url=DETAIL_URL,
            target_status=200,
            transport_status=200,
            text=CHALLENGE_HTML,
            source="scraperapi",
            header_source="provider",
            status_source="provider",
        ),
    )

    row = recordcity_probe._probe_external(
        DETAIL_URL,
        kind="detail",
        strategy="scraperapi",
        timeout_seconds=5,
    )

    assert row["target_status"] is None
    assert row["challenge"] is True
    assert row["outcome"] == "external_block_source_ambiguous"
    assert row["reason"] == "RC_EXTERNAL_BLOCK_SOURCE_AMBIGUOUS"


def test_external_provider_redirect_cannot_be_reported_as_product_success(monkeypatch):
    from services.recordcity_external_fetch import RecordCityExternalResponse

    monkeypatch.setattr(
        "services.recordcity_external_fetch.fetch_recordcity_external",
        lambda *_args, **_kwargs: RecordCityExternalResponse(
            url=DETAIL_URL,
            target_status=302,
            transport_status=302,
            text=PRODUCT_HTML,
            source="scraperapi",
            header_source="provider",
            status_source="provider",
        ),
    )

    row = recordcity_probe._probe_external(
        DETAIL_URL,
        kind="detail",
        strategy="scraperapi",
        timeout_seconds=5,
    )

    assert row["target_status"] is None
    assert row["transport_status"] == 302
    assert row["ready_dom"] is False
    assert row["product_json_ld"] is False
    assert row["outcome"] == "external_provider_error"
    assert row["reason"] == "RC_EXTERNAL_PROVIDER_HTTP_ERROR"


def test_assessment_supports_browser_mode_factor_only_with_waf_control():
    assessment = recordcity_probe._assess_results(
        [
            _with_browser_signal(_waf_failure("patchright-current")),
            _with_browser_signal(_attempted("patchright-headful")),
        ]
    )

    assert assessment["code"] == "browser_mode_factor_supported"


def test_assessment_does_not_infer_browser_mode_from_mismatched_ua_profiles():
    assessment = recordcity_probe._assess_results(
        [
            _with_browser_signal(
                _waf_failure("patchright-current"),
                user_agent=HEADLESS_UA,
                headless=True,
            ),
            _with_browser_signal(_attempted("patchright-headful")),
        ]
    )

    assert assessment["code"] == "inconclusive"


def test_assessment_supports_headless_user_agent_factor():
    current = _with_browser_signal(
        _waf_failure("patchright-current"),
        user_agent=HEADLESS_UA,
        headless=True,
    )
    headless_ua = _with_browser_signal(_attempted("patchright-headless-ua"))

    assessment = recordcity_probe._assess_results([current, headless_ua])

    assert assessment["code"] == "headless_user_agent_factor_supported"


def test_assessment_does_not_infer_ua_factor_from_identical_ua_profiles():
    current = _with_browser_signal(_waf_failure("patchright-current"))
    headless_ua = _with_browser_signal(_attempted("patchright-headless-ua"))

    assessment = recordcity_probe._assess_results([current, headless_ua])

    assert assessment["code"] == "inconclusive"


def test_assessment_supports_render_egress_factor_with_complete_matrix():
    assessment = recordcity_probe._assess_results(
        [
            _with_browser_signal(_waf_failure("patchright-current")),
            _with_browser_signal(_waf_failure("patchright-headful")),
            _with_browser_signal(_attempted("patchright-headless-proxy")),
            _with_browser_signal(_attempted("patchright-headful-proxy")),
        ]
    )

    assert assessment["code"] == "render_egress_factor_supported"


def test_assessment_supports_browser_and_egress_interaction():
    assessment = recordcity_probe._assess_results(
        [
            _with_browser_signal(_waf_failure("patchright-current")),
            _with_browser_signal(_waf_failure("patchright-headful")),
            _with_browser_signal(_waf_failure("patchright-headless-proxy")),
            _with_browser_signal(_attempted("patchright-headful-proxy")),
        ]
    )

    assert assessment["code"] == "browser_and_egress_interaction_supported"


def test_assessment_does_not_infer_egress_factor_from_mismatched_ua_profiles():
    assessment = recordcity_probe._assess_results(
        [
            _with_browser_signal(_waf_failure("patchright-current")),
            _with_browser_signal(_waf_failure("patchright-headful")),
            _with_browser_signal(
                _attempted("patchright-headless-proxy"),
                user_agent=HEADLESS_UA,
                headless=True,
            ),
            _with_browser_signal(_attempted("patchright-headful-proxy")),
        ]
    )

    assert assessment["code"] == "inconclusive"


def test_assessment_does_not_treat_launch_error_as_waf_evidence():
    failed = recordcity_probe._base_result("patchright-current")
    failed.update({"attempted": True, "outcome": "error", "error_type": "RuntimeError"})

    assessment = recordcity_probe._assess_results(
        [failed, _attempted("patchright-headful")]
    )

    assert assessment["code"] == "inconclusive"


def test_assessment_does_not_treat_generic_403_as_waf_evidence():
    generic_403 = _attempted("patchright-current", outcome="blocked_403")
    generic_403["target_status"] = 403
    generic_403["failure_reason"] = "RC_WAF_BLOCK_403"

    assessment = recordcity_probe._assess_results(
        [generic_403, _attempted("patchright-headful")]
    )

    assert assessment["code"] == "inconclusive"


def test_headful_cell_is_skipped_without_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        recordcity_probe,
        "_probe_browser",
        lambda *_args, **_kwargs: pytest.fail("headful browser must not start"),
    )

    snapshot = recordcity_probe.run_recordcity_probe(
        DETAIL_URL,
        strategies=["patchright-headful"],
        delay_seconds=0,
    )

    row = snapshot["results"][0]
    assert row["attempted"] is False
    assert row["reason"] == "display_not_configured_use_xvfb_run"


@pytest.mark.parametrize(
    "strategy, env_name, env_value, probe_attr",
    [
        ("zyte", "RECORDCITY_ZYTE_API_KEY", "secret", "_probe_external"),
        (
            "patchright-headless-proxy",
            "RECORDCITY_PROXY_URL",
            "http://proxy.example:8080",
            "_probe_browser",
        ),
    ],
)
def test_external_strategies_require_allow_flag(
    monkeypatch,
    strategy,
    env_name,
    env_value,
    probe_attr,
):
    calls = []
    monkeypatch.setenv(env_name, env_value)
    monkeypatch.setattr(
        recordcity_probe,
        probe_attr,
        lambda _url, **kwargs: (
            calls.append(kwargs["strategy"])
            or _attempted(kwargs["strategy"])
        ),
    )

    blocked = recordcity_probe.run_recordcity_probe(
        DETAIL_URL,
        strategies=[strategy],
        delay_seconds=0,
        allow_external=False,
    )
    allowed = recordcity_probe.run_recordcity_probe(
        DETAIL_URL,
        strategies=[strategy],
        delay_seconds=0,
        allow_external=True,
    )

    assert blocked["results"][0]["attempted"] is False
    assert blocked["results"][0]["reason"] == "external_requires_allow_flag"
    assert allowed["results"][0]["attempted"] is True
    assert calls == [strategy]


def test_cli_emits_table_and_machine_readable_json(app, monkeypatch):
    snapshot = {
        "probe_id": "deadbeef",
        "results": [_attempted("curl-chrome120")],
    }
    monkeypatch.setattr(recordcity_probe, "run_recordcity_probe", lambda *_args, **_kwargs: snapshot)

    result = app.test_cli_runner().invoke(
        args=[
            "recordcity-probe",
            DETAIL_URL,
            "--strategy",
            "curl-chrome120",
            "--delay-seconds",
            "3",
        ]
    )

    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert "strategy" in lines[0]
    assert json.loads(lines[-1])["probe_id"] == "deadbeef"


def test_cli_forwards_allow_external(app, monkeypatch):
    captured = {}

    def fake_probe(*_args, **kwargs):
        captured.update(kwargs)
        return {"probe_id": "deadbeef", "results": []}

    monkeypatch.setattr(recordcity_probe, "run_recordcity_probe", fake_probe)

    result = app.test_cli_runner().invoke(
        args=[
            "recordcity-probe",
            DETAIL_URL,
            "--strategy",
            "zyte",
            "--allow-external",
            "--json-only",
        ]
    )

    assert result.exit_code == 0
    assert captured["allow_external"] is True
    assert captured["strategies"] == ("zyte",)
