from database import SessionLocal
from models import Product, Variant

def delete_product():
    session = SessionLocal()
    try:
        url = "https://jp.mercari.com/shops/product/xhF79ibHz3KwucSXev32C5"
        # Normalize logic from app.py
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        normalized_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        
        product = session.query(Product).filter_by(source_url=normalized_url).first()
        if product:
            print(f"Deleting product ID {product.id}")
            # delete variants
            session.query(Variant).filter_by(product_id=product.id).delete()
            session.delete(product)
            session.commit()
            print("Product deleted.")
        else:
            print("Product not found to delete.")
    finally:
        session.close()

if __name__ == "__main__":
    delete_product()
