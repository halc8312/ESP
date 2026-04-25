from services.detail_field_strategy_runner import DetailFieldStrategy, run_detail_field_strategies


def test_run_detail_field_strategies_prefers_first_valid_value():
    value, source = run_detail_field_strategies(
        DetailFieldStrategy("next_data", ""),
        DetailFieldStrategy("meta", "Meta Title"),
        DetailFieldStrategy("css", "CSS Title"),
        default="",
    )

    assert value == "Meta Title"
    assert source == "meta"


def test_run_detail_field_strategies_supports_lazy_resolvers():
    calls = []

    def lazy_value():
        calls.append("lazy")
        return ["https://example.com/image.jpg"]

    value, source = run_detail_field_strategies(
        DetailFieldStrategy("next_data", []),
        DetailFieldStrategy("meta", resolver=lazy_value),
        validator=lambda candidate: isinstance(candidate, list) and len(candidate) > 0,
        default=[],
    )

    assert value == ["https://example.com/image.jpg"]
    assert source == "meta"
    assert calls == ["lazy"]


def test_run_detail_field_strategies_uses_default_when_all_candidates_fail():
    value, source = run_detail_field_strategies(
        DetailFieldStrategy("meta", ""),
        DetailFieldStrategy("css", None),
        default=[],
    )

    assert value == []
    assert source == ""
