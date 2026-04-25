import pytest
from flask import url_for
from models import User
from werkzeug.security import generate_password_hash
from services.password_policy import validate_password_strength

def test_login_page_loads(client):
    response = client.get('/login')
    assert response.status_code == 200
    assert b"Login" in response.data

def test_index_requires_login(client):
    response = client.get('/', follow_redirects=True)
    # expect redirect to login
    assert len(response.history) > 0
    assert response.request.path == "/login"

def test_login_flow(client, db_session):
    # Setup user
    password = "password123"
    user = User(username="testadmin")
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    
    # Login
    response = client.post('/login', data={'username': 'testadmin', 'password': password}, follow_redirects=True)
    assert response.status_code == 200
    assert response.request.path == "/" # Should be at index
    
    # Check access to protected route
    response = client.get('/scrape')
    assert response.status_code == 200
    
    # Logout
    response = client.get('/logout', follow_redirects=True)
    assert response.request.path == "/login"
    
    # Check protected route again
    response = client.get('/', follow_redirects=True)
    assert response.request.path == "/login"


def test_register_rejects_weak_password(client):
    response = client.post('/register', data={'username': 'weakuser', 'password': 'password123'})

    assert response.status_code == 400
    assert b'too common' in response.data


def test_register_can_be_disabled(client):
    client.application.config['ALLOW_PUBLIC_SIGNUP'] = False

    response = client.get('/register')

    assert response.status_code == 403
    assert b'Public signup is disabled' in response.data


def test_login_failed_attempts_are_rate_limited(client, db_session):
    client.application.config['LOGIN_RATE_LIMIT'] = 2
    user = User(username='ratelimituser')
    user.set_password('CorrectPassword123')
    db_session.add(user)
    db_session.commit()

    for _ in range(2):
        response = client.post('/login', data={'username': 'ratelimituser', 'password': 'wrong'})
        assert response.status_code == 200

    response = client.post('/login', data={'username': 'ratelimituser', 'password': 'wrong'})

    assert response.status_code == 429
    assert b'Too many attempts' in response.data


def test_register_attempts_are_rate_limited(client):
    client.application.config['REGISTER_RATE_LIMIT'] = 1

    first = client.post('/register', data={'username': 'shortpass', 'password': 'short'})
    assert first.status_code == 400

    second = client.post('/register', data={'username': 'anotheruser', 'password': 'StrongPassword123'})
    assert second.status_code == 429
    assert b'Too many attempts' in second.data


def test_password_policy_requires_length_and_digit():
    assert validate_password_strength('short1', username='tester')
    assert validate_password_strength('NoDigitsHere', username='tester')
    assert validate_password_strength('testerStrong123', username='tester')
    assert validate_password_strength('StrongPassword123', username='tester') == []
