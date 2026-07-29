from models import PricingRule, Product, User


def _create_and_login_user(client, db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )
    return user


def _rule_form(**overrides):
    values = {
        "name": "Standard rule",
        "margin_rate": "30",
        "shipping_cost": "500",
        "fixed_fee": "100",
    }
    values.update(overrides)
    return values


def test_pricing_rule_create_rejects_invalid_or_negative_values(
    client,
    db_session,
):
    user = _create_and_login_user(
        client,
        db_session,
        "pricing_rule_create_validation_user",
    )

    invalid_responses = [
        client.post(
            "/pricing/create",
            data=_rule_form(name="", margin_rate="30"),
            follow_redirects=True,
        ),
        client.post(
            "/pricing/create",
            data=_rule_form(name="Bad margin", margin_rate="not-a-number"),
            follow_redirects=True,
        ),
        client.post(
            "/pricing/create",
            data=_rule_form(name="Negative shipping", shipping_cost="-1"),
            follow_redirects=True,
        ),
    ]

    assert all(response.status_code == 200 for response in invalid_responses)
    assert "ルール名を入力してください".encode() in invalid_responses[0].data
    assert "利益上乗せ率は0以上の整数".encode() in invalid_responses[1].data
    assert "送料上乗せは0以上の整数".encode() in invalid_responses[2].data
    assert (
        db_session.query(PricingRule)
        .filter_by(user_id=user.id)
        .count()
        == 0
    )


def test_pricing_rule_edit_validation_does_not_partially_mutate_or_reprice(
    client,
    db_session,
):
    user = _create_and_login_user(
        client,
        db_session,
        "pricing_rule_edit_validation_user",
    )
    rule = PricingRule(
        user_id=user.id,
        name="Original rule",
        margin_rate=20,
        shipping_cost=500,
        fixed_fee=100,
    )
    db_session.add(rule)
    db_session.flush()
    product = Product(
        user_id=user.id,
        site="manual",
        source_url="https://example.invalid/pricing-validation",
        last_title="Pricing validation product",
        last_price=1000,
        selling_price=1900,
        pricing_rule_id=rule.id,
    )
    db_session.add(product)
    db_session.commit()

    response = client.post(
        f"/pricing/{rule.id}/edit",
        data=_rule_form(
            name="MUTATED",
            margin_rate="40",
            shipping_cost="INVALID",
            fixed_fee="200",
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "送料上乗せは0以上の整数".encode() in response.data
    db_session.expire_all()
    refreshed_rule = db_session.get(PricingRule, rule.id)
    refreshed_product = db_session.get(Product, product.id)
    assert refreshed_rule.name == "Original rule"
    assert refreshed_rule.margin_rate == 20
    assert refreshed_rule.shipping_cost == 500
    assert refreshed_rule.fixed_fee == 100
    assert refreshed_product.selling_price == 1900


def test_pricing_rule_accepts_explicit_zero_values(client, db_session):
    user = _create_and_login_user(
        client,
        db_session,
        "pricing_rule_zero_validation_user",
    )

    response = client.post(
        "/pricing/create",
        data=_rule_form(
            name="Zero rule",
            margin_rate="0",
            shipping_cost="0",
            fixed_fee="0",
        ),
    )

    assert response.status_code == 302
    rule = (
        db_session.query(PricingRule)
        .filter_by(user_id=user.id, name="Zero rule")
        .one()
    )
    assert rule.margin_rate == 0
    assert rule.shipping_cost == 0
    assert rule.fixed_fee == 0
