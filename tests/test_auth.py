import pytest
from models import User
import uuid

def test_login_page_loads(client):
    response = client.get('/login')
    assert response.status_code == 200
    assert "ログイン".encode("utf-8") in response.data

def test_index_requires_login(client):
    response = client.get('/', follow_redirects=True)
    # expect redirect to login
    assert len(response.history) > 0
    assert response.request.path == "/login"

def test_login_flow(client, db_session):
    # Setup user
    password = "password123"
    username = f"testadmin_{uuid.uuid4().hex[:8]}"
    user = User(username=username)
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    
    # Login
    response = client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)
    assert response.status_code == 200
    assert response.request.path == "/" # Should be at index
    
    # Check access to protected route
    response = client.get('/scrape')
    assert response.status_code == 200

    response = client.get('/account')
    assert response.status_code == 200
    assert "アカウント".encode("utf-8") in response.data
    
    # Logout
    response = client.get('/logout', follow_redirects=True)
    assert response.request.path == "/login"
    
    # Check protected route again
    response = client.get('/', follow_redirects=True)
    assert response.request.path == "/login"


def test_change_password_from_account_page(client, db_session):
    password = "password123"
    updated_password = "updatedpassword123"
    username = f"accountuser_{uuid.uuid4().hex[:8]}"

    user = User(username=username)
    user.set_password(password)
    db_session.add(user)
    db_session.commit()

    client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)

    response = client.post(
        '/account',
        data={
            'current_password': password,
            'new_password': updated_password,
            'confirm_password': updated_password,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "パスワードを変更しました".encode("utf-8") in response.data

    client.get('/logout', follow_redirects=True)

    response = client.post('/login', data={'username': username, 'password': updated_password}, follow_redirects=True)
    assert response.status_code == 200
    assert response.request.path == "/"
