"""
CLI commands for the application.
"""
import traceback
from datetime import datetime

from database import SessionLocal
from models import Product, Variant, ProductSnapshot
from mercari_db import scrape_single_item


def register_cli_commands(app):
    """Register CLI commands with the Flask app."""
    
    @app.cli.command("update-products")
    def update_products():
        """全商品の価格と在庫ステータスを再チェックして更新するCLIコマンド"""
        import time
        
        session_db = SessionLocal()
        try:
            products = session_db.query(Product).filter(Product.status != 'sold').all()
            total = len(products)
            print(f"Start updating {total} products...")
            
            updated_count = 0
            
            for i, product in enumerate(products, 1):
                url = product.source_url
                print(f"[{i}/{total}] ShopID:{product.shop_id} | Checking: {url}")
                
                try:
                    items = scrape_single_item(url, headless=True)
                    if not items:
                        print(f"  -> Failed to scrape.")
                        continue
                        
                    item = items[0]
                    new_price = item.get("price")
                    new_status = item.get("status") or "unknown"
                    new_title = item.get("title") or ""
                    
                    price_changed = (new_price is not None) and (product.last_price != new_price)
                    status_changed = (new_status != "unknown") and (product.last_status != new_status)
                    
                    if price_changed or status_changed:
                        print(f"  -> CHANGED! Price: {product.last_price}->{new_price}, Status: {product.last_status}->{new_status}")
                        
                        product.last_price = new_price
                        product.last_status = new_status
                        product.last_title = new_title 
                        product.updated_at = datetime.utcnow()
                        
                        default_variant = session_db.query(Variant).filter_by(
                            product_id=product.id, 
                            option1_value="Default Title"
                        ).first()
                        
                        if default_variant:
                            if new_price is not None:
                                default_variant.price = new_price
                            default_variant.inventory_qty = 0 if new_status == 'sold' else (default_variant.inventory_qty or 1)

                        snapshot = ProductSnapshot(
                            product_id=product.id,
                            scraped_at=datetime.utcnow(),
                            title=new_title,
                            price=new_price,
                            status=new_status,
                            description=item.get("description") or "",
                            image_urls="|".join(item.get("image_urls") or [])
                        )
                        session_db.add(snapshot)
                        updated_count += 1
                    else:
                        print("  -> No change.")
                        
                    time.sleep(2)
                    
                except Exception as e:
                    print(f"  -> Error: {e}")
                    traceback.print_exc()
                    
            session_db.commit()
            print(f"Finished. Total updated: {updated_count}")
            
        finally:
            session_db.close()
