import requests
import sys
import uuid

def verify_registration():
    base_url = "http://localhost:5000"
    session = requests.Session()
    
    # 1. Register new user (Random ID)
    username = f"user_{uuid.uuid4().hex[:8]}"
    password = "password123"
    
    print(f"Registering user {username}...")
    register_url = f"{base_url}/register"
    res = session.post(register_url, data={"username": username, "password": password})
    
    print(f"Register Status: {res.status_code}")
    print(f"Current URL: {res.url}")
    
    # Check for index text
    if res.status_code == 200 and "商品一覧" in res.text:
         print("Registration success (Auto-login verified by content).")
    elif "Username already exists" in res.text:
         print("User already exists (unexpected for random user).")
         return False
    else:
         print(f"Registration failed. Content preview: {res.text[:100]}...")
         return False
         
    # 2. Logout
    print("Logging out...")
    session.get(f"{base_url}/logout")
    
    # 3. Login again
    print("Logging in again...")
    login_url = f"{base_url}/login"
    res = session.post(login_url, data={"username": username, "password": password})
    
    if res.status_code == 200 and "商品一覧" in res.text:
        print("Login with new user PASSED")
        return True
    else:
        print("Login with new user FAILED")
        print(res.text[:200])
        return False

if __name__ == "__main__":
    if verify_registration():
        sys.exit(0)
    else:
        sys.exit(1)
