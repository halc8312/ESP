import sys
import os
import requests
import time

# Add current dir to path for imports
sys.path.append(os.getcwd())

# Import DB logic to check results directly
from database import SessionLocal
from models import Product, Variant, User

def verify_db():
    session = SessionLocal()
    try:
        # Check if product exists
        url = "https://jp.mercari.com/shops/product/xhF79ibHz3KwucSXev32C5"
        # Normalize logic from app.py
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        normalized_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        
        print(f"Checking product with URL: {normalized_url}")
        
        product = session.query(Product).filter_by(source_url=normalized_url).first()
        
        if not product:
            print("Product not found in DB.")
            return False
            
        print(f"Product found: ID={product.id}, Title={product.last_title}")
        print(f"Option1 Name: {product.option1_name}")
        print(f"Option2 Name: {product.option2_name}")
        
        variants = session.query(Variant).filter_by(product_id=product.id).all()
        print(f"Found {len(variants)} variants.")
        
        if len(variants) == 0:
            print("No variants found.")
            return False
            
        for v in variants:
            print(f" - Variant: {v.option1_value} / {v.option2_value}, Stock={v.inventory_qty}, Price={v.price}")
            
        # We expect multiple variants for this Shops item
        if len(variants) > 1 and product.option1_name:
             print("SUCCESS: Variants are populated.")
             return True
        else:
             print("FAILURE: Variants not populated correctly (Single variant or missing option names).")
             return False

    except Exception as e:
        print(f"Error checking DB: {e}")
        return False
    finally:
        session.close()

if __name__ == "__main__":
    base_url = "http://localhost:5000"
    
    # 1. Login
    s = requests.Session()
    login_url = f"{base_url}/login"
    login_data = {"username": "admin", "password": "password"}
    print(f"Logging in to {login_url}...")
    try:
        r = s.post(login_url, data=login_data)
        print(f"Login Status: {r.status_code}")
        if r.status_code != 200 or "Login" in r.text: # If redirected back to login page implies failure if not 302 handling
             # Flask redirect is 302, requests follows by default. 
             # If successful, we land on index (title "Product List"?)
             if "Login" in r.text and "Invalid" in r.text:
                 print("Login failed invalid credentials.")
                 sys.exit(1)
    except Exception as e:
        print(f"Detailed connection error: {e}")
        sys.exit(1)

    # 2. Scrape
    scrape_url = f"{base_url}/scrape/run"
    target_url = "https://jp.mercari.com/shops/product/xhF79ibHz3KwucSXev32C5"
    data = {"target_url": target_url}
    
    print(f"Sending scrape request to {scrape_url} for {target_url}...")
    try:
        # Increase timeout significantly as scraping headless takes time
        r = s.post(scrape_url, data=data, timeout=120)
        print(f"Scrape Status: {r.status_code}")
    except requests.exceptions.Timeout:
        print("Scrape request timed out (it might still be running server-side).")
    except Exception as e:
        print(f"Scrape request failed: {e}")

    # 3. Verify DB
    if verify_db():
        print("Verification PASSED")
        sys.exit(0)
    else:
        print("Verification FAILED")
        sys.exit(1)
