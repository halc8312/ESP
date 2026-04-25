from models import Product, User
from time_utils import utc_now


def _create_user(db_session, username):
    user = User(username=username)
    user.set_password('testpassword')
    db_session.add(user)
    db_session.commit()
    return user


def test_update_products_dry_run_uses_site_specific_scraper(app, client, db_session, monkeypatch):
    user = _create_user(db_session, 'cli_update_products_user')
    product = Product(
        user_id=user.id,
        site='yahoo',
        source_url='https://store.shopping.yahoo.co.jp/test/item.html',
        last_title='Old Yahoo Item',
        last_price=1000,
        last_status='on_sale',
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()

    called = []

    def fake_yahoo_scraper(url, headless=True):
        called.append(("yahoo", url, headless))
        return [{
            'url': url,
            'title': 'New Yahoo Item',
            'price': 1200,
            'status': 'active',
            'description': '',
            'image_urls': [],
        }]

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("Mercari scraper should not be used for Yahoo products")

    monkeypatch.setattr(
        "cli._get_single_item_scrapers",
        lambda: {
            'mercari': should_not_be_called,
            'yahoo': fake_yahoo_scraper,
        },
    )

    runner = app.test_cli_runner()
    result = runner.invoke(args=['update-products', '--site', 'yahoo', '--dry-run'])

    assert result.exit_code == 0
    assert called == [('yahoo', 'https://store.shopping.yahoo.co.jp/test/item.html', True)]
