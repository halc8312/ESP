from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

class User(UserMixin, Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Shop(Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False) # 所有者
    name = Column(String, nullable=False) # ユーザーごとにユニークであれば良いが、シンプルにグローバルユニークのままにするか、user_idと複合ユニークにするか。一旦nameはグローバルユニークの制約を外す方が無難だが、Existing logic relies on name. Let's keep name unique for now or just remove unique constraint if we want same shop names for diff users. Let's start with simple: user_id added.

    name = Column(String, nullable=False) 
    created_at = Column(DateTime, default=datetime.utcnow)
    
    products = relationship("Product", back_populates="shop")
    user = relationship("User") # Link to User


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False) # 所有者
    site = Column(String, nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=True) # 店舗ID
    source_url = Column(String, nullable=False, index=True) # Global unique might be tricky if two users scrape same item. Remove unique constraint on source_url to allow multiple users to track same item independently.

    shop = relationship("Shop", back_populates="products")
    user = relationship("User")

    # スクレイピング情報の履歴・キャッシュ（代表値として保持）
    last_title = Column(String)
    last_price = Column(Integer)
    last_status = Column(String)

    # ユーザーによる編集内容 (Product Level)
    custom_title = Column(String)
    custom_description = Column(Text)
    
    # Shopify項目 (Product Level)
    status = Column(String, default='draft') # active or draft
    custom_vendor = Column(String)
    custom_handle = Column(String)
    tags = Column(String) # comma separated
    seo_title = Column(String)
    seo_description = Column(String)
    
    # Options (Variant管理用)
    option1_name = Column(String, default="Title")
    option2_name = Column(String)
    option3_name = Column(String)

    # Pricing
    pricing_rule_id = Column(Integer, ForeignKey("pricing_rules.id"), nullable=True)
    selling_price = Column(Integer)  # Calculated selling price

    # Archive (SOLD Stacking)
    archived = Column(Boolean, default=False)

    # Trash (Soft Delete)
    deleted_at = Column(DateTime, nullable=True)  # NULL = not deleted

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    snapshots = relationship("ProductSnapshot", back_populates="product", cascade="all, delete-orphan")
    variants = relationship("Variant", back_populates="product", cascade="all, delete-orphan")




class Variant(Base):
    __tablename__ = "variants"
    
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    
    # Option Values
    option1_value = Column(String, default="Default Title")
    option2_value = Column(String)
    option3_value = Column(String)
    
    # Variant Specifics
    sku = Column(String)
    price = Column(Integer) # Variant Price
    inventory_qty = Column(Integer, default=0)
    grams = Column(Integer)
    taxable = Column(Boolean, default=False)
    country_of_origin = Column(String)
    hs_code = Column(String)
    
    # 管理用
    position = Column(Integer, default=1)
    
    product = relationship("Product", back_populates="variants")


class ProductSnapshot(Base):
    __tablename__ = "product_snapshots"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    title = Column(String)
    price = Column(Integer)
    status = Column(String)
    description = Column(Text)
    image_urls = Column(Text)

    product = relationship("Product", back_populates="snapshots")


class DescriptionTemplate(Base):
    __tablename__ = "description_templates"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PricingRule(Base):
    """
    Pricing rule for calculating selling prices from scraped cost prices.
    Formula: selling_price = (cost_price + shipping_cost) * (1 + margin_rate) + fixed_fee
    """
    __tablename__ = "pricing_rules"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)  # e.g., "Default", "High Margin"

    # Calculation Parameters
    margin_rate = Column(Integer, default=30)  # Percentage (30 = 30%)
    shipping_cost = Column(Integer, default=0)  # Fixed shipping to add (JPY)
    fixed_fee = Column(Integer, default=0)  # Fixed fee to add (JPY)

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


class ExclusionKeyword(Base):
    """
    Exclusion keywords for filtering out unwanted products during scraping.
    Products containing these keywords in their title will not be saved.
    """
    __tablename__ = "exclusion_keywords"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    keyword = Column(String, nullable=False)
    match_type = Column(String, default="partial")  # "partial" or "exact"

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")

