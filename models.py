from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import relationship
from database import Base
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from time_utils import utc_now

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
    logo_url = Column(String, nullable=True) # ショップロゴ画像URL
    created_at = Column(DateTime, default=utc_now)
    
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
    custom_title_en = Column(String)
    custom_description_en = Column(Text)
    
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

    # Patrol failure tracking (exponential backoff)
    patrol_fail_count = Column(Integer, default=0)

    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now)

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
    scraped_at = Column(DateTime, default=utc_now)

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
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


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

    created_at = Column(DateTime, default=utc_now)

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

    created_at = Column(DateTime, default=utc_now)

    user = relationship("User")


class PriceList(Base):
    """顧客向け価格表（カタログ）"""
    __tablename__ = "price_lists"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)                # 例: "Customer A Price List"
    token = Column(String, unique=True, nullable=False)  # UUID公開アクセス用
    is_active = Column(Boolean, default=True)            # 有効/無効
    currency_rate = Column(Integer, default=150)         # JPY→USD換算レート
    layout = Column(String, default="grid")              # grid / editorial
    notes = Column(Text)                                 # 備考（顧客へのメッセージ等）
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now)

    user = relationship("User")
    items = relationship("PriceListItem", back_populates="price_list", cascade="all, delete-orphan")
    page_views = relationship("CatalogPageView", back_populates="price_list", cascade="all, delete-orphan")


class PriceListItem(Base):
    """価格表に含める商品"""
    __tablename__ = "price_list_items"

    id = Column(Integer, primary_key=True)
    price_list_id = Column(Integer, ForeignKey("price_lists.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    visible = Column(Boolean, default=True)           # 表示/非表示制御
    custom_price = Column(Integer, nullable=True)     # 個別価格（NULLならselling_price使用）
    sort_order = Column(Integer, default=0)

    price_list = relationship("PriceList", back_populates="items")
    product = relationship("Product")


class CatalogPageView(Base):
    """公開カタログの簡易アクセスログ"""
    __tablename__ = "catalog_page_views"

    id = Column(Integer, primary_key=True)
    pricelist_id = Column(Integer, ForeignKey("price_lists.id"), nullable=False)
    viewed_at = Column(DateTime, default=utc_now, index=True)

    ip_hash = Column(String(64))
    user_agent_short = Column(String(32))
    referrer_domain = Column(String(255))
    product_id = Column(Integer, nullable=True)

    price_list = relationship("PriceList", back_populates="page_views")


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"

    job_id = Column(String(64), primary_key=True)
    logical_job_id = Column(String(64), index=True)
    parent_job_id = Column(String(64), ForeignKey("scrape_jobs.job_id"), nullable=True)

    status = Column(String(32), nullable=False, index=True)
    site = Column(String(32), nullable=False, index=True)
    mode = Column(String(32), nullable=False)
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    request_payload = Column(Text)
    context_payload = Column(Text)
    progress_current = Column(Integer)
    progress_total = Column(Integer)
    result_summary = Column(Text)
    result_payload = Column(Text)
    error_message = Column(Text)
    error_payload = Column(Text)
    tracker_dismissed_at = Column(DateTime, index=True)

    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, default=utc_now, nullable=False, index=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User")
    parent = relationship("ScrapeJob", remote_side=[job_id], backref="attempts")
    events = relationship("ScrapeJobEvent", back_populates="job", cascade="all, delete-orphan")


class ScrapeJobEvent(Base):
    __tablename__ = "scrape_job_events"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(64), ForeignKey("scrape_jobs.job_id"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    payload = Column(Text)
    created_at = Column(DateTime, default=utc_now, nullable=False, index=True)

    job = relationship("ScrapeJob", back_populates="events")
