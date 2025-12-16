
from app import SessionLocal, Product, Variant
session_db = SessionLocal()
try:
    print("--- ADIDAS ---")
    products = session_db.query(Product).filter(Product.last_title.like('%adidas%')).all()
    for p in products:
        opt1_val = p.variants[0].option1_value if p.variants else "NoVariant"
        print(f"ID:{p.id} | Title:{p.last_title[:20]} | Opt1Name:{p.option1_name} | Opt1Val:{opt1_val}")

    print("\n--- IPHONE (Failed) ---")
    products = session_db.query(Product).filter(Product.last_title.like('%iPhone%')).limit(5).all()
    for p in products:
         opt1_val = p.variants[0].option1_value if p.variants else "NoVariant"
         print(f"ID:{p.id} | Title:{p.last_title[:20]} | Opt1Name:{p.option1_name} | Opt1Val:{opt1_val}")

finally:
    session_db.close()
