import pytest
from flask import url_for
from models import User
from werkzeug.security import generate_password_hash

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
