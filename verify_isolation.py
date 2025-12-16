import requests
import sys
import uuid

BASE_URL = "http://localhost:5000"

def register_and_login():
    session = requests.Session()
    username = f"user_{uuid.uuid4().hex[:8]}"
    password = "password123"
    
    # Register/Login
    session.post(f"{BASE_URL}/register", data={"username": username, "password": password})
    return session, username

def verify_isolation():
    print("--- Starting Isolation Verification ---")
    
    # User A
    session_a, user_a = register_and_login()
    print(f"User A ({user_a}) logged in.")
    
    # User A creates a Shop
    shop_name_a = f"Shop_A_{uuid.uuid4().hex[:4]}"
    session_a.post(f"{BASE_URL}/shops", data={"name": shop_name_a})
    print(f"User A created shop: {shop_name_a}")
    
    # Verify User A sees Shop A
    res_a = session_a.get(f"{BASE_URL}/shops")
    if shop_name_a not in res_a.text:
         print("FAIL: User A cannot see their own shop.")
         return False
         
    # User B
    session_b, user_b = register_and_login()
    print(f"User B ({user_b}) logged in.")
    
    # Verify User B does NOT see Shop A
    res_b = session_b.get(f"{BASE_URL}/shops")
    if shop_name_a in res_b.text:
        print("FAIL: User B can see User A's shop!")
        return False
    else:
        print("SUCCESS: User B cannot see User A's shop.")
        
    # User B creates Shop B
    shop_name_b = f"Shop_B_{uuid.uuid4().hex[:4]}"
    session_b.post(f"{BASE_URL}/shops", data={"name": shop_name_b})
    print(f"User B created shop: {shop_name_b}")
    
    # Verify User B sees Shop B but not A
    res_b_2 = session_b.get(f"{BASE_URL}/shops")
    if shop_name_b in res_b_2.text and shop_name_a not in res_b_2.text:
        print("SUCCESS: User B sees only their shop.")
    else:
        print("FAIL: User B visibility issue.")
        return False

    # Check User A again
    res_a_2 = session_a.get(f"{BASE_URL}/shops")
    if shop_name_a in res_a_2.text and shop_name_b not in res_a_2.text:
        print("SUCCESS: User A sees only their shop.")
    else:
         print("FAIL: User A visibility issue.")
         return False
         
    return True

if __name__ == "__main__":
    if verify_isolation():
        print("--- Verification PASSED ---")
        sys.exit(0)
    else:
        print("--- Verification FAILED ---")
        sys.exit(1)
