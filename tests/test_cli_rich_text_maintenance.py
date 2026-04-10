import json

from models import DescriptionTemplate, PriceList, Product, ProductSnapshot, User
from services.rich_text import normalize_rich_text


def _load_last_json_line(output: str) -> dict:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return json.loads(lines[-1])


def _create_dirty_records(db_session, suffix: str):
    user = User(username=f"richtext-{suffix}")
    user.set_password("test")
    db_session.add(user)
    db_session.flush()

    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/{suffix}",
        custom_title=f"Product {suffix}",
        custom_description="<p>Hello</p><span>World</span>",
        custom_description_en="Line one\nLine two",
    )
    db_session.add(product)
    db_session.flush()

    snapshot = ProductSnapshot(
        product_id=product.id,
        title=f"Snapshot {suffix}",
        description="<p>Snap</p><script>alert(1)</script>",
    )
    db_session.add(snapshot)

    pricelist = PriceList(
        user_id=user.id,
        name=f"List {suffix}",
        token=f"token-{suffix}",
        notes="<p>Note</p><span>Extra</span>",
        is_active=True,
    )
    db_session.add(pricelist)
    db_session.commit()

    return user, product, snapshot, pricelist


def test_rich_text_maintenance_cli_dry_run_reports_changes_without_applying(app, db_session):
    user, product, snapshot, pricelist = _create_dirty_records(db_session, "dry-run")
    template = DescriptionTemplate(
        name="Dirty Template Dry Run",
        content="<p>Template</p><script>bad()</script>",
    )
    db_session.add(template)
    db_session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["rich-text-maintenance"])

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["mode"] == "dry-run"
    assert payload["ready"] is True
    assert payload["changed_rows"] >= 4
    assert payload["sections"]["description_templates"]["changed"] == 1

    db_session.refresh(product)
    db_session.refresh(snapshot)
    db_session.refresh(pricelist)
    db_session.refresh(template)
    assert product.custom_description == "<p>Hello</p><span>World</span>"
    assert snapshot.description == "<p>Snap</p><script>alert(1)</script>"
    assert pricelist.notes == "<p>Note</p><span>Extra</span>"
    assert template.content == "<p>Template</p><script>bad()</script>"


def test_rich_text_maintenance_cli_apply_scopes_to_user_records(app, db_session):
    user_one, product_one, snapshot_one, pricelist_one = _create_dirty_records(db_session, "user-one")
    user_two, product_two, snapshot_two, pricelist_two = _create_dirty_records(db_session, "user-two")
    template = DescriptionTemplate(
        name="Dirty Template Scoped",
        content="<p>Template</p><script>bad()</script>",
    )
    db_session.add(template)
    db_session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "rich-text-maintenance",
            "--apply",
            "--user-id",
            str(user_one.id),
        ]
    )

    assert result.exit_code == 0
    payload = _load_last_json_line(result.output)
    assert payload["mode"] == "apply"
    assert payload["warnings"] == ["description_templates_skipped_for_user_scope"]
    assert payload["sections"]["products"]["changed"] == 1
    assert payload["sections"]["price_lists"]["changed"] == 1
    assert payload["sections"]["product_snapshots"]["changed"] == 1

    db_session.refresh(product_one)
    db_session.refresh(snapshot_one)
    db_session.refresh(pricelist_one)
    db_session.refresh(product_two)
    db_session.refresh(snapshot_two)
    db_session.refresh(pricelist_two)
    db_session.refresh(template)

    assert product_one.custom_description == normalize_rich_text("<p>Hello</p><span>World</span>")
    assert product_one.custom_description_en == normalize_rich_text("Line one\nLine two")
    assert snapshot_one.description == normalize_rich_text("<p>Snap</p><script>alert(1)</script>")
    assert pricelist_one.notes == normalize_rich_text("<p>Note</p><span>Extra</span>")

    assert product_two.custom_description == "<p>Hello</p><span>World</span>"
    assert snapshot_two.description == "<p>Snap</p><script>alert(1)</script>"
    assert pricelist_two.notes == "<p>Note</p><span>Extra</span>"
    assert template.content == "<p>Template</p><script>bad()</script>"
