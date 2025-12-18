"""
Product service for database operations related to scraped items.
"""
import hashlib
from datetime import datetime
from flask import session, has_request_context

from database import SessionLocal
from models import Product, Variant, ProductSnapshot
from utils import normalize_url


def save_scraped_items_to_db(items, user_id: int, site: str = "mercari"):
    """
    mercari_db.scrape_search_result() が返した items(list[dict]) を
    Product / ProductSnapshot に保存する。
    """
    if not items:
        return 0, 0

    session_db = SessionLocal()
    new_count = 0
    updated_count = 0
    now = datetime.utcnow()

    # contextからcurrent_shop_idを取得（Flask-Loginが必要）など
    # ここでは session から取る形にする
    current_shop_id = session.get('current_shop_id') if has_request_context() else None

    try:
        for item in items:
            raw_url = item.get("url", "")
            if not raw_url:
                continue

            url = normalize_url(raw_url)

            title = item.get("title") or ""
            price = item.get("price")
            status = item.get("status") or ""
            description = item.get("description") or ""
            image_urls = item.get("image_urls") or []
            image_urls_str = "|".join(image_urls)

            # 既存の Product を検索 (User + URL)
            product = session_db.query(Product).filter_by(source_url=url, user_id=user_id).one_or_none()

            if product is None:
                # SKU自動生成 (MER- + URLのMD5ハッシュ先頭10文字)
                sku_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:10].upper()
                generated_sku = f"MER-{sku_hash}"

                # 新規作成
                product = Product(
                    user_id=user_id, # 所有者
                    site=site,
                    shop_id=current_shop_id, # 現在のショップIDを紐付け
                    source_url=url,
                    last_title=title,
                    last_price=price,
                    last_status=status,
                    created_at=now,
                    updated_at=now,
                )
                session_db.add(product)
                session_db.flush()  # ID 発行
                new_count += 1
                
                # バリエーション作成
                scraped_variants = item.get("variants")
                if scraped_variants:
                    # メルカリShopsなどでバリエーションが取得できた場合
                    # オプション名を設定
                    product.option1_name = item.get("option1_name", "Variation")
                    product.option2_name = item.get("option2_name") 
                    product.option3_name = item.get("option3_name")

                    for i, v_data in enumerate(scraped_variants, 1):
                        new_variant = Variant(
                            product_id=product.id,
                            option1_value=v_data.get("option1_value", f"Option {i}"),
                            option2_value=v_data.get("option2_value"), # 追加
                            option3_value=v_data.get("option3_value"), # 追加
                            sku=f"{generated_sku}-{i}", # SKUをユニーク化
                            price=v_data.get("price", price),
                            taxable=False,
                            inventory_qty=v_data.get("inventory_qty", 1),
                            position=i
                        )
                        session_db.add(new_variant)
                else:
                    # 通常商品（単一バリエーション）
                    default_variant = Variant(
                        product_id=product.id,
                        option1_value="Default Title",
                        sku=generated_sku,
                        price=price,
                        taxable=False,
                        inventory_qty=1 if status != 'sold' else 0,
                        position=1
                    )
                    session_db.add(default_variant)

            else:
                # 更新
                product.last_title = title
                product.last_price = price
                product.last_status = status
                product.updated_at = now
                updated_count += 1
                
                # Default Titleバリエーションがあれば価格と在庫を同期
                default_variant = session_db.query(Variant).filter_by(
                    product_id=product.id, 
                    option1_value="Default Title"
                ).first()
                
                if default_variant:
                    if price is not None:
                        default_variant.price = price
                    default_variant.inventory_qty = 0 if status == 'sold' else (default_variant.inventory_qty or 1)

            snapshot = ProductSnapshot(
                product_id=product.id,
                scraped_at=now,
                title=title,
                price=price,
                status=status,
                description=description,
                image_urls=image_urls_str,
            )
            session_db.add(snapshot)

        session_db.commit()
        return new_count, updated_count
    except Exception as e:
        session_db.rollback()
        print("DB 保存エラー:", e)
        return 0, 0
    finally:
        session_db.close()
