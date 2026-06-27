from io import BytesIO

from models import Product, Shop, User


def _login_user(client, db_session, username):
    client.get('/logout')

    user = User(username=username)
    user.set_password('testpassword')
    db_session.add(user)
    db_session.commit()

    client.post('/login', data={
        'username': username,
        'password': 'testpassword',
    })
    return user


def _csv_upload(content='title,price,url\nImported Product,1200,https://example.com/item/1\n'):
    return (BytesIO(content.encode('utf-8')), 'products.csv')


def test_direct_csv_import_rejects_other_users_shop(client, db_session):
    owner = _login_user(client, db_session, 'import_shop_owner')
    foreign_shop = Shop(name='Foreign Import Shop', user_id=owner.id)
    db_session.add(foreign_shop)
    db_session.commit()

    user = _login_user(client, db_session, 'import_shop_attacker')

    response = client.post('/import/csv', data={
        'shop_id': str(foreign_shop.id),
        'site': 'import',
        'file': _csv_upload(),
    }, content_type='multipart/form-data', follow_redirects=True)

    assert response.status_code == 200
    assert db_session.query(Product).filter_by(user_id=user.id, last_title='Imported Product').count() == 0
    assert db_session.query(Product).filter_by(shop_id=foreign_shop.id, last_title='Imported Product').count() == 0


def test_direct_csv_import_allows_owned_shop(client, db_session):
    user = _login_user(client, db_session, 'import_owned_shop_user')
    shop = Shop(name='Owned Import Shop', user_id=user.id)
    db_session.add(shop)
    db_session.commit()

    response = client.post('/import/csv', data={
        'shop_id': str(shop.id),
        'site': 'import',
        'file': _csv_upload(),
    }, content_type='multipart/form-data', follow_redirects=True)

    assert response.status_code == 200
    product = db_session.query(Product).filter_by(user_id=user.id, last_title='Imported Product').one()
    assert product.shop_id == shop.id


def test_csv_preview_stores_only_token_in_session_and_executes(client, db_session):
    user = _login_user(client, db_session, 'import_preview_user')
    shop = Shop(name='Preview Import Shop', user_id=user.id)
    db_session.add(shop)
    db_session.commit()

    response = client.post('/import/preview', data={
        'shop_id': str(shop.id),
        'site': 'import',
        'file': _csv_upload(),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    with client.session_transaction() as flask_session:
        assert 'import_csv_content' not in flask_session
        assert flask_session.get('import_csv_token')
        assert flask_session.get('import_shop_id') == str(shop.id)

    response = client.post('/import/execute', follow_redirects=True)

    assert response.status_code == 200
    product = db_session.query(Product).filter_by(user_id=user.id, last_title='Imported Product').one()
    assert product.shop_id == shop.id
    with client.session_transaction() as flask_session:
        assert 'import_csv_token' not in flask_session
        assert 'import_csv_content' not in flask_session


def test_csv_preview_rejects_other_users_shop_without_session_token(client, db_session):
    owner = _login_user(client, db_session, 'import_preview_owner')
    foreign_shop = Shop(name='Foreign Preview Shop', user_id=owner.id)
    db_session.add(foreign_shop)
    db_session.commit()

    _login_user(client, db_session, 'import_preview_attacker')

    response = client.post('/import/preview', data={
        'shop_id': str(foreign_shop.id),
        'site': 'import',
        'file': _csv_upload(),
    }, content_type='multipart/form-data', follow_redirects=True)

    assert response.status_code == 200
    with client.session_transaction() as flask_session:
        assert 'import_csv_token' not in flask_session
        assert 'import_csv_content' not in flask_session
