from models import PricingRule, Product, ProductSnapshot, Shop, User, Variant
from services.product_service import save_scraped_items_to_db
from time_utils import utc_now



def _create_user(db_session, username):
    user = User(username=username)
    user.set_password('testpassword')
    db_session.add(user)
    db_session.commit()
    return user



def test_save_scraped_items_uses_explicit_shop_id_without_request_context(client, db_session):
    user = _create_user(db_session, 'product_service_shop_user')
    shop = Shop(name='Background Shop', user_id=user.id)
    db_session.add(shop)
    db_session.commit()

    items = [
        {
            'url': 'https://jp.mercari.com/item/m-shop-aware?ref=campaign',
            'title': 'Shop-Aware Item',
            'price': 1800,
            'status': 'on_sale',
            'description': 'saved from background task',
            'image_urls': ['https://img.example.com/shop-aware.jpg'],
        }
    ]

    new_count, updated_count = save_scraped_items_to_db(
        items,
        user_id=user.id,
        site='mercari',
        shop_id=shop.id,
    )

    assert new_count == 1
    assert updated_count == 0

    db_session.expire_all()
    product = db_session.query(Product).filter_by(user_id=user.id).one()
    assert product.shop_id == shop.id
    assert product.source_url == 'https://jp.mercari.com/item/m-shop-aware'



def test_save_scraped_items_recalculates_selling_price_when_cost_changes(client, db_session):
    user = _create_user(db_session, 'product_service_pricing_user')
    rule = PricingRule(
        user_id=user.id,
        name='Half Margin',
        margin_rate=50,
        shipping_cost=0,
        fixed_fee=0,
    )
    db_session.add(rule)
    db_session.commit()

    product = Product(
        user_id=user.id,
        site='mercari',
        source_url='https://jp.mercari.com/item/m-priced',
        last_title='Original Title',
        last_price=1000,
        last_status='on_sale',
        pricing_rule_id=rule.id,
        selling_price=1500,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()

    variant = Variant(
        product_id=product.id,
        option1_value='Default Title',
        sku='MER-PRICE',
        price=1000,
        inventory_qty=1,
        position=1,
    )
    db_session.add(variant)
    db_session.commit()

    items = [
        {
            'url': 'https://jp.mercari.com/item/m-priced?foo=bar',
            'title': 'Updated Title',
            'price': 2000,
            'status': 'on_sale',
            'description': 'repriced item',
            'image_urls': ['https://img.example.com/priced.jpg'],
        }
    ]

    new_count, updated_count = save_scraped_items_to_db(
        items,
        user_id=user.id,
        site='mercari',
    )

    assert new_count == 0
    assert updated_count == 1

    db_session.expire_all()
    refreshed_product = db_session.query(Product).filter_by(id=product.id).one()
    refreshed_variant = db_session.query(Variant).filter_by(product_id=product.id, option1_value='Default Title').one()

    assert refreshed_product.last_title == 'Updated Title'
    assert refreshed_product.last_price == 2000
    assert refreshed_product.selling_price == 3000
    assert refreshed_variant.price == 2000


def test_save_scraped_items_rejects_new_deleted_item(client, db_session):
    user = _create_user(db_session, 'product_service_deleted_new_user')

    items = [
        {
            'url': 'https://jp.mercari.com/item/m-deleted-new',
            'title': '',
            'price': None,
            'status': 'deleted',
            'description': '',
            'image_urls': [],
            '_scrape_meta': {'confidence': 'high', 'reasons': ['missing-marker']},
        }
    ]

    new_count, updated_count = save_scraped_items_to_db(
        items,
        user_id=user.id,
        site='mercari',
    )

    assert new_count == 0
    assert updated_count == 0
    assert db_session.query(Product).filter_by(user_id=user.id).count() == 0


def test_save_scraped_items_allows_status_only_deleted_update(client, db_session):
    user = _create_user(db_session, 'product_service_deleted_existing_user')
    product = Product(
        user_id=user.id,
        site='mercari',
        source_url='https://jp.mercari.com/item/m-deleted-existing',
        last_title='Original Title',
        last_price=3500,
        last_status='on_sale',
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(product)
    db_session.commit()

    variant = Variant(
        product_id=product.id,
        option1_value='Default Title',
        sku='MER-DEL',
        price=3500,
        inventory_qty=1,
        position=1,
    )
    db_session.add(variant)
    db_session.add(
        ProductSnapshot(
            product_id=product.id,
            title='Original Title',
            price=3500,
            status='on_sale',
            description='desc',
            image_urls='https://img.example.com/original.jpg',
        )
    )
    db_session.commit()

    items = [
        {
            'url': 'https://jp.mercari.com/item/m-deleted-existing',
            'title': '',
            'price': None,
            'status': 'deleted',
            'description': '',
            'image_urls': [],
            '_scrape_meta': {'confidence': 'high', 'reasons': ['missing-marker']},
        }
    ]

    new_count, updated_count = save_scraped_items_to_db(
        items,
        user_id=user.id,
        site='mercari',
    )

    assert new_count == 0
    assert updated_count == 1

    db_session.expire_all()
    refreshed_product = db_session.query(Product).filter_by(id=product.id).one()
    refreshed_variant = db_session.query(Variant).filter_by(product_id=product.id).one()
    snapshots = db_session.query(ProductSnapshot).filter_by(product_id=product.id).all()

    assert refreshed_product.last_title == 'Original Title'
    assert refreshed_product.last_price == 3500
    assert refreshed_product.last_status == 'deleted'
    assert refreshed_variant.inventory_qty == 0
    assert len(snapshots) == 1


def test_save_scraped_items_does_not_persist_scrape_meta_fields(client, db_session):
    user = _create_user(db_session, 'product_service_meta_isolation_user')

    items = [
        {
            'url': 'https://www.suruga-ya.jp/product/detail/1',
            'title': 'Metadata Isolation Item',
            'price': 1980,
            'status': 'active',
            'description': 'stored description',
            'image_urls': ['https://img.example.com/surugaya-meta.jpg'],
            '_scrape_meta': {
                'strategy': 'json_ld',
                'field_sources': {
                    'title': 'json_ld',
                    'price': 'css',
                    'description': 'meta',
                },
            },
        }
    ]

    new_count, updated_count = save_scraped_items_to_db(
        items,
        user_id=user.id,
        site='surugaya',
    )

    assert new_count == 1
    assert updated_count == 0

    db_session.expire_all()
    product = db_session.query(Product).filter_by(user_id=user.id).one()
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product.id).one()

    assert product.last_title == 'Metadata Isolation Item'
    assert snapshot.description == 'stored description'
    assert snapshot.image_urls == 'https://img.example.com/surugaya-meta.jpg'
    assert '_scrape_meta' not in product.__dict__
    assert '_scrape_meta' not in snapshot.__dict__


def test_save_scraped_items_normalizes_string_prices_and_variant_quantities(client, db_session):
    user = _create_user(db_session, 'product_service_price_string_user')

    items = [
        {
            'url': 'https://store.shopping.yahoo.co.jp/example/item-string-price.html?tracking=1',
            'title': 'String Price Item',
            'price': '¥1,980',
            'status': 'active',
            'description': 'stored from yahoo',
            'image_urls': ['https://img.example.com/string-price.jpg'],
            'variants': [
                {
                    'option1_name': 'Color',
                    'option1_value': 'Black',
                    'price': '2,100',
                    'inventory_qty': '3',
                }
            ],
        }
    ]

    new_count, updated_count = save_scraped_items_to_db(
        items,
        user_id=user.id,
        site='yahoo',
    )

    assert new_count == 1
    assert updated_count == 0

    db_session.expire_all()
    product = db_session.query(Product).filter_by(user_id=user.id).one()
    variant = db_session.query(Variant).filter_by(product_id=product.id).one()

    assert product.source_url == 'https://store.shopping.yahoo.co.jp/example/item-string-price.html'
    assert product.last_price == 1980
    assert product.last_status == 'on_sale'
    assert variant.price == 2100
    assert variant.inventory_qty == 3


def test_save_scraped_items_keeps_same_source_url_isolated_by_shop(client, db_session):
    user = _create_user(db_session, 'product_service_shop_scope_user')
    source_shop = Shop(name='Source Shop', user_id=user.id)
    target_shop = Shop(name='Target Shop', user_id=user.id)
    db_session.add_all([source_shop, target_shop])
    db_session.commit()

    existing = Product(
        user_id=user.id,
        site='mercari',
        shop_id=source_shop.id,
        source_url='https://jp.mercari.com/item/m-shop-scope',
        last_title='Existing Shop Item',
        last_price=1000,
        last_status='on_sale',
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add(existing)
    db_session.commit()

    items = [
        {
            'url': 'https://jp.mercari.com/item/m-shop-scope?utm=preview',
            'title': 'Target Shop Item',
            'price': 1600,
            'status': 'on_sale',
            'description': 'same source in another shop',
            'image_urls': ['https://img.example.com/shop-scope.jpg'],
        }
    ]

    new_count, updated_count = save_scraped_items_to_db(
        items,
        user_id=user.id,
        site='mercari',
        shop_id=target_shop.id,
    )

    assert new_count == 1
    assert updated_count == 0

    db_session.expire_all()
    products = db_session.query(Product).filter_by(user_id=user.id).order_by(Product.shop_id.asc()).all()
    assert len(products) == 2
    assert {product.shop_id for product in products} == {source_shop.id, target_shop.id}
    assert db_session.query(Product).filter_by(shop_id=source_shop.id).one().last_title == 'Existing Shop Item'
    assert db_session.query(Product).filter_by(shop_id=target_shop.id).one().last_title == 'Target Shop Item'

