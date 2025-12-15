from flask import Flask, render_template_string, request, make_response, send_from_directory, redirect, url_for, session, has_request_context
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    text,
    Boolean,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, subqueryload
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit  # URLパラメータ & 正規化用
import csv
import io
import os
import requests
import shutil
import hashlib

# mercari_db.py からスクレイピング関数を import
from mercari_db import scrape_search_result, scrape_single_item

# ==============================
# SQLAlchemy モデル定義
# ==============================

Base = declarative_base()


class Shop(Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    products = relationship("Product", back_populates="shop")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    site = Column(String, nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=True) # 店舗ID
    source_url = Column(String, nullable=False, unique=True, index=True)

    shop = relationship("Shop", back_populates="products")

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


# ==============================
# DB 接続設定（WAL 有効）
# ==============================

# Renderの永続ディスクを利用する場合、そのパスを環境変数で指定します。
# 環境変数 `DATABASE_URL` が設定されていればそれを使用し、
# なければローカル開発用にカレントディレクトリの `mercari.db` を使用します。
database_url = os.environ.get("DATABASE_URL", "sqlite:///mercari.db")

# --- デバッグ用ログ ---
# Renderのログで実際にどのデータベースパスが使われているか確認します。
print(f"DEBUG: Using database URL: {database_url}")
# --- ここまで ---

engine = create_engine(database_url, echo=False)

# SQLiteの場合のみWALモードを有効化
if "sqlite" in engine.url.drivername:
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))

SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

# ==============================
# ユーティリティ
# ==============================


def normalize_url(raw_url: str) -> str:
    """?以降のクエリを落として正規化したURLを返す"""
    try:
        parts = urlsplit(raw_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return raw_url


def save_scraped_items_to_db(items, site: str = "mercari"):
    """
    mercari_db.scrape_search_result() が返した items(list[dict]) を
    Product / ProductSnapshot に保存する。
    """
    session_db = SessionLocal()
    now = datetime.utcnow()
    new_count = 0
    updated_count = 0

    try:
        current_shop_id = None
        if has_request_context():
            current_shop_id = session.get('current_shop_id')

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

            # 既存の Product を検索
            product = session_db.query(Product).filter_by(source_url=url).one_or_none()

            if product is None:
                # SKU自動生成 (MER- + URLのMD5ハッシュ先頭10文字)
                sku_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:10].upper()
                generated_sku = f"MER-{sku_hash}"

                # 新規作成
                product = Product(
                    site=site,
                    shop_id=current_shop_id, # 現在のショップIDを紐付け
                    source_url=url,
                    last_title=title,
                    last_price=price,
                    last_status=status,
                    # sku=generated_sku, # Productには持たせない
                    # taxable=False,     # Productには持たせない
                    created_at=now,
                    updated_at=now,
                )
                session_db.add(product)
                session_db.flush()  # ID 発行
                new_count += 1
                
                # デフォルトバリエーション作成
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
                # (ユーザーがバリエーション構成を変えていない場合のみ追従させる)
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


# ==============================
# Flask アプリ
# ==============================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")

# ==============================
# テンプレート管理
# ==============================

SHOPS_TEMPLATE = """
<!doctype html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <title>ショップ管理</title>
    <style>
        body { font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
        table { border-collapse: collapse; width: 100%; margin-top: 20px; }
        th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
        th { background: #f0f0f0; }
        .nav { margin-bottom: 20px; padding: 10px; background: #eee; border-radius: 5px; display: flex; align-items: center; justify-content: space-between; }
        .nav a { margin-right: 15px; font-weight: bold; text-decoration: none; color: #333; }
        .current-shop { font-weight: bold; color: #0066cc; }
    </style>
</head>
<body>
    <div class="nav">
        <div>
            <a href="{{ url_for('index') }}">商品一覧</a>
            <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
            <a href="{{ url_for('manage_templates') }}">テンプレート</a>
            <a href="{{ url_for('manage_shops') }}">ショップ管理</a>
        </div>
        <div>
            <form action="{{ url_for('set_current_shop') }}" method="post" style="margin:0;">
                <select name="shop_id" onchange="this.form.submit()">
                    <option value="">(ショップ未選択)</option>
                    {% for s in all_shops %}
                        <option value="{{ s.id }}" {% if current_shop_id == s.id %}selected{% endif %}>
                            {{ s.name }}
                        </option>
                    {% endfor %}
                </select>
            </form>
        </div>
    </div>

    <h1>ショップ管理</h1>

    <div style="border:1px solid #ccc; padding:15px; background:#f9f9f9;">
        <h3>新規ショップ追加</h3>
        <form method="POST">
            <input type="text" name="name" placeholder="ショップ名 (例: 文具店A)" required>
            <button type="submit">追加</button>
        </form>
    </div>

    <h3>登録済みショップ</h3>
    <table>
        <tr>
            <th>ID</th>
            <th>ショップ名</th>
            <th>商品数</th>
            <th>操作</th>
        </tr>
        {% for s in shops %}
        <tr>
            <td>{{ s.id }}</td>
            <td>{{ s.name }}</td>
            <td>{{ s.product_count }}</td>
            <td>
                <form method="POST" action="{{ url_for('delete_shop', shop_id=s.id) }}" style="display:inline;">
                    <button type="submit" onclick="return confirm('削除すると紐付いている商品も影響を受けます。本当によろしいですか？');">削除</button>
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

TEMPLATES_TEMPLATE = """
<!doctype html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <title>説明文テンプレート管理</title>
    <style>
        body { font-family: sans-serif; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ccc; padding: 4px 8px; font-size: 13px; vertical-align: top; }
        th { background: #f0f0f0; }
        .nav { margin-bottom: 20px; padding: 10px; background: #eee; border-radius: 5px; display: flex; align-items: center; justify-content: space-between; }
        .nav a { margin-right: 15px; font-weight: bold; text-decoration: none; color: #333; }
        .template-form {
            border: 1px solid #ccc;
            padding: 16px;
            margin-top: 16px;
        }
    </style>
</head>
<body>
    <div class="nav">
        <div>
            <a href="{{ url_for('index') }}">商品一覧</a>
            <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
            <a href="{{ url_for('manage_templates') }}">テンプレート</a>
            <a href="{{ url_for('manage_shops') }}">ショップ管理</a>
        </div>
        <div>
            <form action="{{ url_for('set_current_shop') }}" method="post" style="margin:0;">
                <select name="shop_id" onchange="this.form.submit()">
                    <option value="">(ショップ未選択)</option>
                    {% for s in all_shops %}
                        <option value="{{ s.id }}" {% if current_shop_id == s.id %}selected{% endif %}>
                            {{ s.name }}
                        </option>
                    {% endfor %}
                </select>
            </form>
        </div>
    </div>

    <h1>説明文テンプレート管理</h1>

    <h2>登録済みテンプレート</h2>
    <table>
        <tr>
            <th>ID</th>
            <th>テンプレート名</th>
            <th>内容 (抜粋)</th>
            <th>操作</th>
        </tr>
        {% for t in templates %}
        <tr>
            <td>{{ t.id }}</td>
            <td>{{ t.name }}</td>
            <td>{{ t.content[:50] if t.content else '' }}...</td>
            <td>
                <form method="POST" action="{{ url_for('delete_template', template_id=t.id) }}" style="display:inline;">
                    <button type="submit" onclick="return confirm('本当に削除しますか？');">削除</button>
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>

    <div class="template-form">
        <h2>新規テンプレート作成</h2>
        <form method="POST" action="{{ url_for('manage_templates') }}">
            <div>
                <label>テンプレート名</label>
                <input type="text" name="name" required style="width: 300px;">
            </div>
            <div style="margin-top: 8px;">
                <label>テンプレート内容</label>
                <textarea name="content" rows="10" required style="width: 100%; box-sizing: border-box;"></textarea>
            </div>
            <div style="margin-top: 8px;">
                <button type="submit">作成</button>
            </div>
        </form>
    </div>
</body>
</html>
"""

from flask import session # Import session

@app.route("/shops", methods=["GET", "POST"])
def manage_shops():
    session_db = SessionLocal()
    try:
        if request.method == "POST":
            name = request.form.get("name")
            if name:
                new_shop = Shop(name=name)
                session_db.add(new_shop)
                try:
                    session_db.commit()
                except Exception:
                    session_db.rollback() # 重複など
            return redirect(url_for('manage_shops'))

        shops = session_db.query(Shop).all()
        # 各ショップの商品数をカウント
        shop_data = []
        for s in shops:
            count = session_db.query(Product).filter_by(shop_id=s.id).count()
            s.product_count = count
            shop_data.append(s)

        current_shop_id = session.get('current_shop_id')
        
        return render_template_string(
            SHOPS_TEMPLATE, 
            shops=shop_data,
            all_shops=shops, # ナビゲーション用
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()

@app.route("/shops/<int:shop_id>/delete", methods=["POST"])
def delete_shop(shop_id):
    session_db = SessionLocal()
    try:
        shop = session_db.query(Shop).filter_by(id=shop_id).one_or_none()
        if shop:
            # 商品の紐付けを解除するか、削除するか？今回は紐付け解除(None)にする
            products = session_db.query(Product).filter_by(shop_id=shop_id).all()
            for p in products:
                p.shop_id = None
            session_db.delete(shop)
            session_db.commit()
            
            if session.get('current_shop_id') == shop_id:
                session.pop('current_shop_id', None)
                
    finally:
        session_db.close()
    return redirect(url_for('manage_shops'))

@app.route("/set_current_shop", methods=["POST"])
def set_current_shop():
    shop_id = request.form.get("shop_id")
    if shop_id:
        session['current_shop_id'] = int(shop_id)
    else:
        session.pop('current_shop_id', None)
    
    # リファラがあればそこに戻る、なければindex
    return redirect(request.referrer or url_for('index'))


@app.route("/templates", methods=["GET", "POST"])
def manage_templates():
    session_db = SessionLocal()
    try:
        if request.method == "POST":
            name = request.form.get("name")
            content = request.form.get("content")
            if name and content:
                new_template = DescriptionTemplate(name=name, content=content)
                session_db.add(new_template)
                session_db.commit()
            return redirect(url_for('manage_templates'))

        templates = session_db.query(DescriptionTemplate).order_by(DescriptionTemplate.id).all()
        
        # ナビゲーション用
        all_shops = session_db.query(Shop).all()
        current_shop_id = session.get('current_shop_id')

        return render_template_string(
            TEMPLATES_TEMPLATE, 
            templates=templates,
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()

@app.route("/templates/<int:template_id>/delete", methods=["POST"])
def delete_template(template_id):
    session = SessionLocal()
    try:
        template = session.query(DescriptionTemplate).filter_by(id=template_id).one_or_none()
        if template:
            session.delete(template)
            session.commit()
    finally:
        session.close()
    return redirect(url_for('manage_templates'))


# ==============================
# 画像保存設定
# ==============================
# Renderの永続ディスクを利用するため、環境変数でパスを指定できるようにします。
# なければローカルの `static/images` を使用します。
IMAGE_STORAGE_PATH = os.environ.get("IMAGE_STORAGE_PATH", os.path.join('static', 'images'))
os.makedirs(IMAGE_STORAGE_PATH, exist_ok=True)

# --- 画像配信用のルート ---
# 永続ディスクに保存した画像を配信するためのエンドポイント
@app.route("/media/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGE_STORAGE_PATH, filename)
# -------------------------


def cache_mercari_image(mercari_url, product_id, index):
    """
    メルカリの画像をダウンロードし、ローカルのファイル名を返す。
    失敗した場合は None を返す。
    """
    if not mercari_url:
        return None
        
    # ファイル名: mercari_{ID}_{連番}.jpg
    filename = f"mercari_{product_id}_{index}.jpg"
    local_path = os.path.join(IMAGE_STORAGE_PATH, filename)
    
    # すでに保存済みならダウンロードしない
    if os.path.exists(local_path):
        return filename

    try:
        # メルカリから画像をダウンロード
        # User-Agentを指定しないと拒否されることがある
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://jp.mercari.com/'
        }
        resp = requests.get(mercari_url, headers=headers, stream=True, timeout=10)
        
        if resp.status_code == 200:
            with open(local_path, 'wb') as f:
                resp.raw.decode_content = True
                shutil.copyfileobj(resp.raw, f)
            return filename
    except Exception as e:
        print(f"Image download failed: {e}")
    
    return None


PAGE_SIZE = 50

# 一覧テンプレート（為替レート＋eBayパラメータ付き）
INDEX_TEMPLATE = """
<!doctype html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <title>商品一覧（mercari.db）</title>
    <style>
        body { font-family: sans-serif; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ccc; padding: 4px 8px; font-size: 13px; vertical-align: top; }
        th { background: #f0f0f0; }
        img { max-height: 80px; }
        .changed { background-color: #fff8e1 !important; } /* 変更があった行をハイライト */
        .actions {
            margin: 8px 0;
            padding: 8px;
            background: #f7f7f7;
            border: 1px solid #ddd;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: flex-start;
        }
        .actions > div {
            min-width: 160px;
            max-width: 230px;
        }
        .actions label {
            display: block;
            font-size: 12px;
            margin-bottom: 2px;
        }
        .actions input[type="number"],
        .actions input[type="text"] {
            width: 100%;
            box-sizing: border-box;
        }
        .filters { margin: 8px 0 16px 0; padding: 8px; background: #eef7ff; border: 1px solid #bcd; }
        .filters label { margin-right: 12px; }
        .pagination { margin-top: 12px; display: flex; gap: 12px; align-items: center; }
        .pagination a { text-decoration: none; color: #06c; }
        .nav { margin-bottom: 20px; padding: 10px; background: #eee; border-radius: 5px; display: flex; align-items: center; justify-content: space-between; }
        .nav a { margin-right: 15px; font-weight: bold; text-decoration: none; color: #333; }
    </style>
</head>
<body>
    <div class="nav">
        <div>
            <a href="{{ url_for('index') }}">商品一覧</a>
            <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
            <a href="{{ url_for('manage_templates') }}">テンプレート</a>
            <a href="{{ url_for('manage_shops') }}">ショップ管理</a>
        </div>
        <div>
            <form action="{{ url_for('set_current_shop') }}" method="post" style="margin:0;">
                <select name="shop_id" onchange="this.form.submit()">
                    <option value="">(ショップ未選択)</option>
                    {% for s in all_shops %}
                        <option value="{{ s.id }}" {% if current_shop_id == s.id %}selected{% endif %}>
                            {{ s.name }}
                        </option>
                    {% endfor %}
                </select>
            </form>
        </div>
    </div>

    <h1>商品一覧（{{ selected_site or "全サイト" }} / {{ selected_status or "全ステータス" }}）</h1>

    <!-- フィルタフォーム -->
    <form method="GET" action="{{ url_for('index') }}">
        <div class="filters">
            <label>
                サイト:
                <select name="site">
                    <option value="">(すべて)</option>
                    {% for s in sites %}
                        <option value="{{ s }}" {% if s == selected_site %}selected{% endif %}>{{ s }}</option>
                    {% endfor %}
                </select>
            </label>
            <label>
                ステータス:
                <select name="status">
                    <option value="">(すべて)</option>
                    {% for st in statuses %}
                        <option value="{{ st }}" {% if st == selected_status %}selected{% endif %}>{{ st }}</option>
                    {% endfor %}
                </select>
            </label>
            <label>
                変更:
                <select name="change_filter">
                    <option value="">(すべて)</option>
                    <option value="changed" {% if selected_change_filter == 'changed' %}selected{% endif %}>変更ありのみ</option>
                </select>
            </label>
            <button type="submit">フィルタ</button>
        </div>
    </form>

    <!-- エクスポート用フォーム -->
    <form method="GET">
        <div class="actions">
            <div>
                <label>価格倍率 (markup)</label>
                <input type="number" id="markup" name="markup" value="{{ default_markup }}" step="0.01" oninput="updateProfitMargin()">
                <span style="font-size: 11px; color: #555;">1.0=そのまま / 1.2=20%上乗せ</span>
            </div>
            <div>
                <label>利益率 (%)</label>
                <input type="number" id="profit_margin" step="1" oninput="updateMarkup()">
                <span style="font-size: 11px; color: #555;">価格倍率と連動します</span>
            </div>
            <div>
                <label>在庫数 (Quantity)</label>
                <input type="number" name="qty" value="{{ default_qty }}" step="1" min="0">
            </div>
            <div>
                <label>為替レート (JPY → USD)</label>
                <input type="number" name="rate" value="{{ default_rate }}" step="0.01">
                <span style="font-size: 11px; color: #555;">1ドルあたりの円（例: 155）</span>
            </div>
            <div>
                <label>eBay カテゴリID (Category)</label>
                <input type="text" name="ebay_category_id" value="{{ default_ebay_category_id }}" placeholder="例: 183454">
            </div>
            <div>
                <label>eBay ConditionID</label>
                <input type="text" name="ebay_condition_id" value="{{ default_ebay_condition_id }}" placeholder="新品=1000 / 中古=3000">
            </div>
            <div>
                <label>PaymentProfileName</label>
                <input type="text" name="ebay_payment_profile" value="{{ default_ebay_payment_profile }}" placeholder="例: payAddress">
            </div>
            <div>
                <label>ReturnProfileName</label>
                <input type="text" name="ebay_return_profile" value="{{ default_ebay_return_profile }}" placeholder="例: return Buyer pays for shipping">
            </div>
            <div>
                <label>ShippingProfileName</label>
                <input type="text" name="ebay_shipping_profile" value="{{ default_ebay_shipping_profile }}" placeholder="例: [US DDP] $132.01–$198">
            </div>
            <div>
                <label>PayPalEmailAddress</label>
                <input type="text" name="ebay_paypal_email" value="{{ default_ebay_paypal_email }}" placeholder="例: your-paypal@example.com">
            </div>

            <div style="flex-basis: 100%; margin-top: 8px;">
                <button type="submit" formaction="{{ url_for('export_shopify') }}">
                    Shopify 風 CSV
                </button>
                <button type="submit" formaction="{{ url_for('export_ebay') }}">
                    eBay File Exchange 用 CSV
                </button>
                <button type="submit" formaction="{{ url_for('export_stock_update') }}">
                    Shopify 在庫更新CSV
                </button>
                <button type="submit" formaction="{{ url_for('export_price_update') }}">
                    Shopify 価格更新CSV
                </button>
            </div>
        </div>

        <table>
            <tr>
                <th>選択</th>
                <th>ID</th>
                <th>サイト</th>
                <th>サムネイル</th>
                <th>商品名</th>
                <th>価格</th>
                <th>ステータス</th>
                <th>画像枚数</th>
                <th>元URL</th>
                <th>詳細</th>
                <th>最終更新</th>
            </tr>
            {% for p in products %}
                <tr class="{{ 'changed' if p.has_changed else '' }}">
                <td>
                    <input type="checkbox" name="id" value="{{ p.id }}">
                </td>
                <td>{{ p.id }}</td>
                <td>{{ p.site }}</td>
                <td>
                    {% if thumb_url %}
                        <img src="{{ thumb_url }}" alt="thumb">
                    {% endif %}
                </td>
                <td>{{ p.last_title }}</td>
                <td>
                    {% if p.last_price is not none %}
                        ¥{{ "{:,}".format(p.last_price) }}
                    {% endif %}
                </td>
                <td>{{ p.last_status }}</td>
                <td>{{ image_count }}</td>
                <td><a href="{{ p.source_url }}" target="_blank">開く</a></td>
                <td><a href="{{ url_for('product_detail', product_id=p.id) }}">編集</a></td>
                <td>{{ p.updated_at }}</td>
            </tr>
            {% endfor %}
        </table>

        <div class="pagination">
            {% if has_prev %}
                <a href="{{ url_for('index', page=page-1, site=selected_site, status=selected_status) }}">&laquo; 前のページ</a>
            {% endif %}
            <span>ページ {{ page }} / {{ total_pages }}</span>
            {% if has_next %}
                <a href="{{ url_for('index', page=page+1, site=selected_site, status=selected_status) }}">次のページ &raquo;</a>
            {% endif %}
        </div>
    </form>
    <script>
        function updateProfitMargin() {
            const markupInput = document.getElementById('markup');
            const profitMarginInput = document.getElementById('profit_margin');
            const markup = parseFloat(markupInput.value);
            if (!isNaN(markup) && markup > 0) {
                // 利益率 = (売上 - 仕入) / 売上
                // markup = 売上 / 仕入
                // 利益率 = (markup - 1) / markup * 100
                const profitMargin = ((markup - 1) / markup) * 100;
                profitMarginInput.value = profitMargin.toFixed(0);
            } else {
                profitMarginInput.value = '';
            }
        }

        function updateMarkup() {
            const markupInput = document.getElementById('markup');
            const profitMarginInput = document.getElementById('profit_margin');
            const profitMargin = parseFloat(profitMarginInput.value);
            if (!isNaN(profitMargin) && profitMargin < 100) {
                // markup = 1 / (1 - (利益率 / 100))
                const markup = 1 / (1 - (profitMargin / 100));
                markupInput.value = markup.toFixed(2);
            } else {
                markupInput.value = '';
            }
        }

        // ページ読み込み時に初期値を設定
        document.addEventListener('DOMContentLoaded', function() {
            // markupの値から利益率を計算して表示
            updateProfitMargin();
        });
    </script>
</body>
</html>
"""

# 詳細テンプレート
DETAIL_TEMPLATE = """
<!doctype html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <title>商品編集 - {{ product.last_title }}</title>
    <style>
        body { font-family: sans-serif; max-width: 1100px; margin: 0 auto; padding-bottom: 50px; }
        .nav { margin-bottom: 20px; padding: 10px; background: #eee; border-radius: 5px; display: flex; align-items: center; justify-content: space-between; }
        .nav a { margin-right: 15px; font-weight: bold; text-decoration: none; color: #333; }
        .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .form-section { border: 1px solid #ccc; padding: 16px; border-radius: 5px; margin-top: 20px; }
        .form-section h2 { margin-top: 0; font-size: 16px; border-bottom: 1px solid #ddd; padding-bottom: 8px; }
        .form-group { margin-bottom: 12px; }
        .form-group label { display: block; font-weight: bold; margin-bottom: 4px; font-size: 13px; }
        .form-group input, .form-group textarea, .form-group select {
            width: 100%;
            box-sizing: border-box;
            padding: 6px;
            font-size: 14px;
        }
        .form-group textarea { min-height: 200px; }
        
        /* バリエーションテーブル用 */
        .variant-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .variant-table th, .variant-table td { border: 1px solid #ddd; padding: 6px; text-align: center; }
        .variant-table th { background: #f9f9f9; }
        .variant-table input[type="text"], .variant-table input[type="number"] { width: 100%; box-sizing: border-box; padding: 4px; }
        .variant-table input.short { width: 60px; }
        .del-btn { background: #d9534f; color: white; border: none; padding: 4px 8px; cursor: pointer; border-radius: 3px; }
        .add-btn { background: #5cb85c; color: white; border: none; padding: 8px 16px; cursor: pointer; border-radius: 3px; margin-top: 8px; font-weight: bold;}
        .deleted-row { display: none; background-color: #fdd; }
        
        .images { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
        .images img { max-width: 150px; max-height: 150px; border: 1px solid #ccc; }
    </style>
</head>
<body>
    <div class="nav">
        <div>
            <a href="{{ url_for('index') }}">商品一覧</a>
            <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
            <a href="{{ url_for('manage_templates') }}">テンプレート</a>
            <a href="{{ url_for('manage_shops') }}">ショップ管理</a>
        </div>
        <div>
            <form action="{{ url_for('set_current_shop') }}" method="post" style="margin:0;">
                <select name="shop_id" onchange="this.form.submit()">
                    <option value="">(ショップ未選択)</option>
                    {% for s in all_shops %}
                        <option value="{{ s.id }}" {% if current_shop_id == s.id %}selected{% endif %}>
                            {{ s.name }}
                        </option>
                    {% endfor %}
                </select>
            </form>
        </div>
    </div>

    <h1>商品編集</h1>
    <p><a href="{{ url_for('index') }}">&laquo; 一覧に戻る</a></p>

    <form method="POST">
        <!-- 削除対象IDリスト -->
        <input type="hidden" name="delete_v_ids" id="delete_v_ids" value="">
        
        <!-- 上部：基本情報 -->
        <div class="form-section">
            <h2>基本情報</h2>
            
            <div class="form-group">
                <label for="shop_id">所属ショップ</label>
                <select id="shop_id" name="shop_id">
                    <option value="">(共通 / 未所属)</option>
                    {% for s in all_shops %}
                        <option value="{{ s.id }}" {% if product.shop_id == s.id %}selected{% endif %}>{{ s.name }}</option>
                    {% endfor %}
                </select>
            </div>
            
            <div class="form-group">
                <label for="title">商品名 (Title)</label>
                <input type="text" id="title" name="title" value="{{ product.custom_title or product.last_title or '' }}">
            </div>

            <div class="form-group">
                <label for="status">ステータス (Status)</label>
                <select id="status" name="status">
                    <option value="active" {% if product.status == 'active' %}selected{% endif %}>有効 (Active)</option>
                    <option value="draft" {% if product.status == 'draft' %}selected{% endif %}>下書き (Draft)</option>
                </select>
            </div>
            
            <div class="form-group">
                <label for="template">説明文テンプレート</label>
                <select id="template" onchange="applyTemplate()">
                    <option value="">(テンプレートを選択)</option>
                    {% for t in templates %}
                    <option value="{{ t.content | e }}">{{ t.name }}</option>
                    {% endfor %}
                </select>
            </div>

            <div class="form-group">
                <label for="description">商品説明 (Body HTML)</label>
                <textarea id="description" name="description">{{ product.custom_description or (snapshot.description if snapshot else '') }}</textarea>
            </div>
        </div>

        <!-- 中段：バリエーション設定 -->
        <div class="form-section">
            <h2>バリエーション設定</h2>
            
            <!-- オプション名の設定 -->
            <div style="display:flex; gap:10px; margin-bottom:10px; background:#f0f0f0; padding:10px;">
                <div>
                    <label style="font-size:11px;">Option1 Name</label>
                    <input type="text" name="option1_name" value="{{ product.option1_name or 'Title' }}" style="width:120px;">
                </div>
                <div>
                    <label style="font-size:11px;">Option2 Name</label>
                    <input type="text" name="option2_name" value="{{ product.option2_name or '' }}" style="width:120px;" placeholder="(例: Size)">
                </div>
                <div>
                    <label style="font-size:11px;">Option3 Name</label>
                    <input type="text" name="option3_name" value="{{ product.option3_name or '' }}" style="width:120px;">
                </div>
            </div>

            <table class="variant-table" id="variantTable">
                <thead>
                    <tr>
                        <th style="width:15%">Opt1 Value</th>
                        <th style="width:15%">Opt2 Value</th>
                        <th style="width:15%">Price</th>
                        <th style="width:15%">SKU</th>
                        <th style="width:10%">在庫</th>
                        <th style="width:10%">重量(g)</th>
                        <th>税 / HS / Origin</th>
                        <th style="width:50px;">操作</th>
                    </tr>
                </thead>
                <tbody>
                    {% for v in variants %}
                    <tr id="row-{{ v.id }}">
                        <td>
                            <input type="hidden" name="v_ids" value="{{ v.id }}">
                            <input type="text" name="v_opt1_{{ v.id }}" value="{{ v.option1_value or '' }}">
                        </td>
                        <td>
                            <input type="text" name="v_opt2_{{ v.id }}" value="{{ v.option2_value or '' }}">
                        </td>
                        <td>
                            <input type="number" name="v_price_{{ v.id }}" value="{{ v.price or '' }}">
                        </td>
                        <td>
                            <input type="text" name="v_sku_{{ v.id }}" value="{{ v.sku or '' }}">
                        </td>
                        <td>
                            <input type="number" name="v_qty_{{ v.id }}" value="{{ v.inventory_qty }}" class="short">
                        </td>
                        <td>
                            <input type="number" name="v_grams_{{ v.id }}" value="{{ v.grams or '' }}" class="short">
                        </td>
                        <td style="text-align:left; font-size:11px;">
                            <label><input type="checkbox" name="v_tax_{{ v.id }}" {% if v.taxable %}checked{% endif %}> 課税</label><br>
                            HS: <input type="text" name="v_hs_{{ v.id }}" value="{{ v.hs_code or '' }}" style="width:60px;"><br>
                            Org: <input type="text" name="v_org_{{ v.id }}" value="{{ v.country_of_origin or '' }}" style="width:40px;">
                        </td>
                        <td>
                            <button type="button" class="del-btn" onclick="markDelete({{ v.id }})">削除</button>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            
            <button type="button" class="add-btn" onclick="addVariantRow()">＋ バリエーションを追加</button>
        </div>

        <!-- 下段：その他 -->
        <div class="form-grid">
            <div class="form-section">
                <h2>分類</h2>
                <div class="form-group">
                    <label for="vendor">販売元 (Vendor)</label>
                    <input type="text" id="vendor" name="vendor" value="{{ product.custom_vendor or product.site or '' }}">
                </div>
                <div class="form-group">
                    <label for="tags">タグ (Tags)</label>
                    <input type="text" id="tags" name="tags" value="{{ product.tags or '' }}" placeholder="カンマ区切りで入力">
                </div>
            </div>

            <div class="form-section">
                <h2>SEO</h2>
                <div class="form-group">
                    <label for="handle">URLハンドル (Handle)</label>
                    <input type="text" id="handle" name="handle" value="{{ product.custom_handle or ('mercari-' + product.id|string) }}">
                </div>
                <div class="form-group">
                    <label for="seo_title">ページタイトル (SEO Title)</label>
                    <input type="text" id="seo_title" name="seo_title" value="{{ product.seo_title or '' }}" placeholder="60文字以内推奨">
                </div>
                <div class="form-group">
                    <label for="seo_description">メタディスクリプション (SEO Description)</label>
                    <textarea id="seo_description" name="seo_description" rows="4">{{ product.seo_description or '' }}</textarea>
                </div>
            </div>
        </div>

        <div style="margin-top: 20px;">
            <button type="submit">保存</button>
        </div>
    </form>

    <div class="form-section">
        <h2>画像（{{ images|length }}枚）</h2>
        {% if images %}
            <div class="images">
                {% for url in images %}
                    <img src="{{ url }}" alt="image {{ loop.index }}">
                {% endfor %}
            {% else %}
            <p>(画像なし)</p>
        {% endif %}
    </div>

    <script>
        function applyTemplate() {
            var select = document.getElementById('template');
            var content = select.value;
            if (content) {
                document.getElementById('description').value = content;
            }
        }

        // 削除対象IDを保持するリスト
        var deleteIds = [];

        function markDelete(id) {
            if (confirm('このバリエーションを削除しますか？（保存時に反映されます）')) {
                deleteIds.push(id);
                document.getElementById('delete_v_ids').value = deleteIds.join(',');
                document.getElementById('row-' + id).style.display = 'none';
                document.getElementById('row-' + id).classList.add('deleted-row');
            }
        }

        var newIndex = 0;

        function addVariantRow() {
            newIndex++;
            var idx = newIndex;
            var table = document.getElementById('variantTable').getElementsByTagName('tbody')[0];
            var newRow = table.insertRow();
            
            newRow.innerHTML = `
                <td>
                    <input type="hidden" name="new_v_indices" value="${idx}">
                    <input type="text" name="new_v_opt1_${idx}" value="">
                </td>
                <td>
                    <input type="text" name="new_v_opt2_${idx}" value="">
                </td>
                <td>
                    <input type="number" name="new_v_price_${idx}" value="">
                </td>
                <td>
                    <input type="text" name="new_v_sku_${idx}" value="">
                </td>
                <td>
                    <input type="number" name="new_v_qty_${idx}" value="1" class="short">
                </td>
                <td>
                    <input type="number" name="new_v_grams_${idx}" value="" class="short">
                </td>
                <td style="text-align:left; font-size:11px;">
                    <label><input type="checkbox" name="new_v_tax_${idx}"> 課税</label><br>
                    HS: <input type="text" name="new_v_hs_${idx}" value="" style="width:60px;"><br>
                    Org: <input type="text" name="new_v_org_${idx}" value="JP" style="width:40px;">
                </td>
                <td>
                    <button type="button" class="del-btn" onclick="removeNewRow(this)">取消</button>
                </td>
            `;
        }

        function removeNewRow(btn) {
            var row = btn.parentNode.parentNode;
            row.parentNode.removeChild(row);
        }
    </script>
</body>
</html>
"

@app.route("/")
def index():
    session_db = SessionLocal()
    try:
        page = int(request.args.get("page", 1))

        # フィルタリング条件の取得
        selected_site = request.args.get("site")
        selected_status = request.args.get("status")
        selected_change_filter = request.args.get("change_filter")

        # サイトとステータスのリストを取得
        sites = [s[0] for s in session_db.query(Product.site).distinct().all()]
        statuses = [s[0] for s in session_db.query(Product.last_status).distinct().all()]
        
        # ナビゲーション用
        all_shops = session_db.query(Shop).all()
        current_shop_id = session.get('current_shop_id')

        # 基本的なクエリ
        query = session_db.query(Product)
        
        # ショップフィルタ
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)
        
        if selected_site:
            query = query.filter(Product.site == selected_site)
        if selected_status:
            query = query.filter(Product.last_status == selected_status)

        # パフォーマンスのためにスナップショットをEager Loadingする
        all_products = query.options(subqueryload(Product.snapshots)).order_by(Product.updated_at.desc()).all()

        # 変更があったかどうかをPython側で判定
        products_to_display = []
        for p in all_products:
            p.has_changed = False
            # スナップショットが2つ以上ないと変更のしようがない
            if len(p.snapshots) >= 2:
                # scraped_atで降順ソート
                sorted_snapshots = sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)
                latest = sorted_snapshots[0]
                previous = sorted_snapshots[1]
                
                # 価格またはステータスが変更されていたらフラグを立てる
                if latest.price != previous.price or latest.status != previous.status:
                    p.has_changed = True
            
            # 「変更ありのみ」フィルタが有効な場合は、変更があった商品だけリストに追加
            if selected_change_filter == 'changed':
                if p.has_changed:
                    products_to_display.append(p)
            else:
                products_to_display.append(p)

        # フィルタ後のリストに対してページネーションを適用
        total_items = len(products_to_display)
        total_pages = (total_items + PAGE_SIZE - 1) // PAGE_SIZE
        offset = (page - 1) * PAGE_SIZE
        
        paginated_products = products_to_display[offset : offset + PAGE_SIZE]

        has_prev = page > 1
        has_next = page < total_pages

        # デフォルト値
        defaults = {
            "markup": request.args.get("markup", "1.2"),
            "qty": request.args.get("qty", "1"),
            "rate": request.args.get("rate", "155"),
            "ebay_category_id": request.args.get("ebay_category_id", ""),
            "ebay_condition_id": request.args.get("ebay_condition_id", "3000"),
            "ebay_payment_profile": request.args.get("ebay_payment_profile", ""),
            "ebay_return_profile": request.args.get("ebay_return_profile", ""),
            "ebay_shipping_profile": request.args.get("ebay_shipping_profile", ""),
            "ebay_paypal_email": request.args.get("ebay_paypal_email", ""),
        }

        return render_template_string(
            INDEX_TEMPLATE,
            products=paginated_products,
            sites=sites,
            statuses=statuses,
            selected_site=selected_site,
            selected_status=selected_status,
            selected_change_filter=selected_change_filter,
            page=page,
            total_pages=total_pages,
            has_prev=has_prev,
            has_next=has_next,
            default_markup=defaults["markup"],
            default_qty=defaults["qty"],
            default_rate=defaults["rate"],
            default_ebay_category_id=defaults["ebay_category_id"],
            default_ebay_condition_id=defaults["ebay_condition_id"],
            default_ebay_payment_profile=defaults["ebay_payment_profile"],
            default_ebay_return_profile=defaults["ebay_return_profile"],
            default_ebay_shipping_profile=defaults["ebay_shipping_profile"],
            default_ebay_paypal_email=defaults["ebay_paypal_email"],
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()


@app.route("/product/<int:product_id>", methods=["GET", "POST"])
def product_detail(product_id):
    session_db = SessionLocal()
    try:
        product = session_db.query(Product).filter_by(id=product_id).one_or_none()
        if not product:
            return "Product not found", 404

        if request.method == "POST":
            # --- 所属ショップ ---
            shop_id_str = request.form.get("shop_id")
            product.shop_id = int(shop_id_str) if shop_id_str else None

            # --- 基本情報 (Product) ---
            product.custom_title = request.form.get("title")
            product.custom_description = request.form.get("description")
            product.status = request.form.get("status")

            # --- オプション名 (Product) ---
            product.option1_name = request.form.get("option1_name")
            product.option2_name = request.form.get("option2_name")
            product.option3_name = request.form.get("option3_name")

            # --- 分類 (Product) ---
            product.custom_vendor = request.form.get("vendor")
            product.tags = request.form.get("tags")

            # --- SEO (Product) ---
            product.custom_handle = request.form.get("handle")
            product.seo_title = request.form.get("seo_title")
            product.seo_description = request.form.get("seo_description")
            
            # --- バリエーション削除 ---
            delete_ids_str = request.form.get("delete_v_ids", "")
            if delete_ids_str:
                for del_id in delete_ids_str.split(","):
                    if del_id.isdigit():
                        v_to_del = session_db.query(Variant).filter_by(id=int(del_id), product_id=product.id).first()
                        if v_to_del:
                            session_db.delete(v_to_del)

            # --- バリエーション更新 (既存) ---
            v_ids = request.form.getlist("v_ids")
            for v_id_str in v_ids:
                try:
                    v_id = int(v_id_str)
                    variant = session_db.query(Variant).filter_by(id=v_id, product_id=product.id).first()
                    if variant:
                        # Option Values
                        variant.option1_value = request.form.get(f"v_opt1_{v_id}")
                        variant.option2_value = request.form.get(f"v_opt2_{v_id}")
                        
                        # Price
                        p_val = request.form.get(f"v_price_{v_id}")
                        variant.price = int(p_val) if p_val and p_val.isdigit() else None
                        
                        # SKU
                        variant.sku = request.form.get(f"v_sku_{v_id}")
                        
                        # Qty
                        q_val = request.form.get(f"v_qty_{v_id}")
                        variant.inventory_qty = int(q_val) if q_val and q_val.isdigit() else 0
                        
                        # Grams
                        g_val = request.form.get(f"v_grams_{v_id}")
                        variant.grams = int(g_val) if g_val and g_val.isdigit() else None
                        
                        # Taxable
                        variant.taxable = (request.form.get(f"v_tax_{v_id}") == 'on')
                        
                        # HS / Origin
                        variant.hs_code = request.form.get(f"v_hs_{v_id}")
                        variant.country_of_origin = request.form.get(f"v_org_{v_id}")
                        
                except ValueError:
                    continue

            # --- バリエーション新規作成 ---
            new_indices = request.form.getlist("new_v_indices")
            for idx in new_indices:
                try:
                    # 必須項目チェック（例えばOption1が空なら追加しないなど）
                    # 今回は空でも追加する方針
                    new_variant = Variant(
                        product_id=product.id,
                        option1_value=request.form.get(f"new_v_opt1_{idx}"),
                        option2_value=request.form.get(f"new_v_opt2_{idx}"),
                        option3_value=request.form.get(f"new_v_opt3_{idx}"), # テンプレートにはinputないが念のため
                        sku=request.form.get(f"new_v_sku_{idx}"),
                        hs_code=request.form.get(f"new_v_hs_{idx}"),
                        country_of_origin=request.form.get(f"new_v_org_{idx}"),
                        taxable=(request.form.get(f"new_v_tax_{idx}") == 'on')
                    )
                    
                    # 数値項目の処理
                    p_val = request.form.get(f"new_v_price_{idx}")
                    if p_val and p_val.isdigit():
                        new_variant.price = int(p_val)
                        
                    q_val = request.form.get(f"new_v_qty_{idx}")
                    if q_val and q_val.isdigit():
                        new_variant.inventory_qty = int(q_val)
                    else:
                        new_variant.inventory_qty = 0 # Default
                        
                    g_val = request.form.get(f"new_v_grams_{idx}")
                    if g_val and g_val.isdigit():
                        new_variant.grams = int(g_val)
                        
                    # position設定（末尾に追加）
                    # 厳密なMAX取得はしていないが、とりあえず追加
                    
                    session_db.add(new_variant)
                    
                except Exception as e:
                    print(f"Error adding variant {idx}: {e}")
                    continue

            # --- 更新日時 ---
            product.updated_at = datetime.utcnow()
            
            session_db.commit()
            return redirect(url_for('product_detail', product_id=product.id))

        snapshot = (
            session_db.query(ProductSnapshot)
            .filter_by(product_id=product.id)
            .order_by(ProductSnapshot.scraped_at.desc())
            .first()
        )
        
        templates = session_db.query(DescriptionTemplate).order_by(DescriptionTemplate.name).all()

        images = []
        if snapshot and snapshot.image_urls:
            images = snapshot.image_urls.split("|")
            
        all_shops = session_db.query(Shop).all()
        current_shop_id = session.get('current_shop_id')
        
        # バリエーション取得 (position順)
        variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()

        return render_template_string(
            DETAIL_TEMPLATE, 
            product=product, 
            snapshot=snapshot, 
            images=images, 
            templates=templates,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
            variants=variants
        )
    finally:
        session_db.close()


# =========================================================
# スクレイピング設定フォーム
# =========================================================

@app.route("/scrape", methods=["GET", "POST"])
def scrape_form():
    html = """
    <html>
    <head>
        <meta charset="utf-8">
        <title>スクレイピング設定</title>
        <style>
            body { font-family: sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; }
            label { display: block; margin-top: 12px; font-weight: bold; }
            input[type='text'], input[type='number'], select {
                width: 100%;
                padding: 8px;
                margin-top: 4px;
                box-sizing: border-box;
            }
            button {
                margin-top: 20px;
                padding: 10px 20px;
                font-size: 16px;
                background: #06c;
                color: #fff;
                border: none;
                cursor: pointer;
            }
            button:hover { background: #0056b3; }
            .box {
                border: 1px solid #ccc;
                padding: 20px;
                background: #f8f8f8;
                margin-top: 20px;
            }
        </style>
    </head>
    <body>
        <h1>スクレイピング実行</h1>
        <p><a href="{{ url_for('index') }}">← 商品一覧に戻る</a></p>

        <div class="box">
            <h3 style="margin-top:0;">方法1: URLを直接指定 (単品抽出)</h3>
            <form method="POST" action="{{ url_for('scrape_run') }}">
                <label>商品URL</label>
                <input type="text" name="target_url" placeholder="https://jp.mercari.com/item/m123456789" required style="width:100%;">
                <button type="submit">URLからスクレイピング実行</button>
            </form>
        </div>

        <div class="box">
            <h3 style="margin-top:0;">方法2: 検索条件から抽出 (一括抽出)</h3>
            <form method="POST" action="{{ url_for('scrape_run') }}">

                <label>キーワード</label>
                <input type="text" name="keyword" value="スニーカー" required>

                <label>価格 min（任意）</label>
                <input type="number" name="price_min">

                <label>価格 max（任意）</label>
                <input type="number" name="price_max">

                <label>ソート順</label>
                <select name="sort">
                    <option value="created_desc">新着順</option>
                    <option value="price_asc">価格：安い順</option>
                    <option value="price_desc">価格：高い順</option>
                </select>

                <label>カテゴリID（任意、メルカリカテゴリID）</label>
                <input type="text" name="category" placeholder="例：1">

                <label>最大取得件数</label>
                <input type="number" name="limit" value="10" min="1">

                <button type="submit">スクレイピング実行</button>
            </form>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/scrape/run", methods=["POST"])
def scrape_run():
    # フォームからの入力を取得
    target_url = request.form.get("target_url")
    
    keyword = request.form.get("keyword", "")
    price_min = request.form.get("price_min")
    price_max = request.form.get("price_max")
    sort = request.form.get("sort", "created_desc")
    category = request.form.get("category")
    limit_str = request.form.get("limit", "10")
    limit = int(limit_str) if limit_str.isdigit() else 10

    search_url = ""
    items = []
    new_count = 0
    updated_count = 0
    error_msg = ""

    if target_url:
        # --- 単品抽出モード ---
        search_url = target_url
        try:
            items = scrape_single_item(target_url, headless=True)
            new_count, updated_count = save_scraped_items_to_db(items, site="mercari")
        except Exception as e:
            error_msg = str(e)
    else:
        # --- 検索抽出モード ---
        # メルカリ用の検索URLを組み立てる
        params = {}
        if keyword:
            params["keyword"] = keyword
        if price_min:
            params["price_min"] = price_min
        if price_max:
            params["price_max"] = price_max
        if sort:
            params["sort"] = sort
        if category:
            params["category_id"] = category

        base = "https://jp.mercari.com/search?"
        query = urlencode(params)
        search_url = base + query

        # ===== ここから実際のスクレイピング処理 =====
        try:
            # headless=True にして、サーバー環境でGUIなしでブラウザを動かす
            items = scrape_search_result(
                search_url=search_url,
                max_items=limit,
                max_scroll=3,
                headless=True,
            )
            new_count, updated_count = save_scraped_items_to_db(items, site="mercari")
        except Exception as e:
            items = []
            new_count = updated_count = 0
            error_msg = str(e)

    # 結果表示
    html = """
    <html>
    <head><meta charset="utf-8"><title>スクレイピング結果</title>
    <style>
        body { font-family: sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }
        table { border-collapse: collapse; width: 100%; margin-top: 16px; }
        th, td { border: 1px solid #ccc; padding: 4px 8px; font-size: 13px; vertical-align: top; }
        th { background: #f0f0f0; }
        .error { color: red; font-weight: bold; }
    </style>
    </head>
    <body>
        <h1>スクレイピング結果</h1>
        <p><a href="{{ url_for('scrape_form') }}">← 条件を変更して再スクレイピング</a> |
           <a href="{{ url_for('index') }}">商品一覧を見る →</a></p>

        <h3>使用した検索URL:</h3>
        <pre style="background:#f0f0f0; padding:12px; overflow-x: auto;">{{ search_url }}</pre>

        <h3>設定パラメータ:</h3>
        <ul>
            <li>キーワード: {{ keyword }}</li>
            <li>価格範囲: {{ price_min }} 〜 {{ price_max }}</li>
            <li>ソート: {{ sort }}</li>
            <li>カテゴリ: {{ category }}</li>
            <li>取得件数(max_items): {{ limit }}</li>
        </ul>

        {% if error_msg %}
            <p class="error">スクレイピング中にエラーが発生しました: {{ error_msg }}</p>
        {% else %}
            <p>スクレイピング取得件数: {{ items|length }} 件</p>
            <p>DB 新規登録: {{ new_count }} 件 / 更新: {{ updated_count }} 件</p>

            {% if items %}
                <h3>取得した商品（プレビュー）</h3>
                <table>
                    <tr>
                        <th>#</th>
                        <th>タイトル</th>
                        <th>価格</th>
                        <th>ステータス</th>
                        <th>URL</th>
                    </tr>
                    {% for it in items %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td>{{ it.title }}</td>
                        <td>
                            {% if it.price is not none %}
                                ¥{{ "{:,}".format(it.price) }}
                            {% else %}
                                -
                            {% endif %}
                        </td>
                        <td>{{ it.status }}</td>
                        <td><a href="{{ it.url }}" target="_blank">開く</a></td>
                    </tr>
                    {% endfor %}
                </table>
            {% endif %}
        {% endif %}
    </body>
    </html>
    """

    return render_template_string(
        html,
        search_url=search_url,
        keyword=keyword,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
        category=category,
        limit=limit,
        items=items,
        new_count=new_count,
        updated_count=updated_count,
        error_msg=error_msg,
    )


# ==============================
# CSVエクスポート機能
# ==============================

def _parse_ids_and_params(session_db):
    ids = request.args.getlist("id")
    markup_str = request.args.get("markup", "1.0")
    qty_str = request.args.get("qty", "1")

    try:
        markup = float(markup_str)
    except ValueError:
        markup = 1.0

    try:
        qty = int(qty_str)
    except ValueError:
        qty = 1

    query = session_db.query(Product)
    
    # ショップフィルタ (セッションに設定されていれば)
    if has_request_context():
        current_shop_id = session.get('current_shop_id')
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)

    if ids:
        int_ids = []
        for v in ids:
            try:
                int_ids.append(int(v))
            except ValueError:
                continue
        if int_ids:
            query = query.filter(Product.id.in_(int_ids))

    products = query.all()
    return products, markup, qty


@app.route("/export/shopify")
def export_shopify():
    """Shopifyの新規登録・更新用のCSVを生成する"""
    session_db = SessionLocal()
    try:
        product_ids = request.args.getlist("id", type=int)
        
        # クエリ構築
        query = session_db.query(Product)
        
        # ショップフィルタ
        current_shop_id = session.get('current_shop_id')
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)
            
        if product_ids:
             query = query.filter(Product.id.in_(product_ids))
        elif not product_ids and not current_shop_id:
             pass

        products = query.all()
        if not products:
            return "対象の商品がありません。", 400

        # 価格倍率・在庫数のデフォルト値を取得
        markup = request.args.get("markup", "1.0", type=float)
        default_qty = request.args.get("qty", "1", type=int)
        
        output = io.StringIO()
        
        # Shopify CSVのヘッダーを定義
        fieldnames = [
            "Handle", "Title", "Body (HTML)", "Vendor", "Status", "Tags",
            "Published", "SEO Title", "SEO Description",
            "Option1 Name", "Option1 Value",
            "Option2 Name", "Option2 Value",
            "Option3 Name", "Option3 Value",
            "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
            "Variant Inventory Qty", "Variant Inventory Policy", "Variant Fulfillment Service",
            "Variant Price", "Variant Requires Shipping", "Variant Taxable",
            "Image Src", "Image Position", "Country of Origin", "HS Code"
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            snapshot = (
                session_db.query(ProductSnapshot)
                .filter_by(product_id=product.id)
                .order_by(ProductSnapshot.scraped_at.desc())
                .first()
            )

            # 基本情報
            title = product.custom_title or product.last_title or ""
            description = product.custom_description or (snapshot.description if snapshot else "")
            vendor = product.custom_vendor or product.site.capitalize()
            handle = product.custom_handle or f"mercari-{product.id}"
            
            # 画像URLリスト
            image_urls = []
            if snapshot and snapshot.image_urls:
                base_url = request.url_root.rstrip('/')
                original_urls = snapshot.image_urls.split("|")
                for i, mercari_url in enumerate(original_urls):
                    local_filename = cache_mercari_image(mercari_url, product.id, i)
                    if local_filename:
                        full_url = f"{base_url}/media/{local_filename}"
                        image_urls.append(full_url)
            
            # バリエーション取得
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            if not variants:
                # 万が一バリエーションがない場合はスキップするか、ダミーを作る
                continue

            # --- バリエーションループ ---
            for i, variant in enumerate(variants):
                row = {}
                row["Handle"] = handle
                
                # 1行目のみ親情報を埋める
                if i == 0:
                    row["Title"] = title
                    row["Body (HTML)"] = description.replace("\\n", "<br>")
                    row["Vendor"] = vendor
                    row["Published"] = "true" if product.status == 'active' else 'false'
                    row["Status"] = product.status
                    row["Tags"] = product.tags or ""
                    row["SEO Title"] = product.seo_title or ""
                    row["SEO Description"] = product.seo_description or ""
                    
                    # オプション名は親情報として1行目に必須
                    row["Option1 Name"] = product.option1_name or "Title"
                    row["Option2 Name"] = product.option2_name or ""
                    row["Option3 Name"] = product.option3_name or ""
                    
                    # 1枚目の画像
                    if image_urls:
                        row["Image Src"] = image_urls[0]
                        row["Image Position"] = 1
                
                # バリエーション情報
                row["Option1 Value"] = variant.option1_value
                row["Option2 Value"] = variant.option2_value
                row["Option3 Value"] = variant.option3_value
                
                row["Variant SKU"] = variant.sku or ""
                row["Variant Grams"] = variant.grams or ""
                row["Variant Inventory Tracker"] = "shopify"
                
                # 在庫: soldなら0、そうでなければ指定値(Default) または Variant個別の値
                # 優先順位: 売り切れ判定(0) > Variant個別設定 > デフォルト指定
                if product.last_status == 'sold':
                    final_qty = 0
                else:
                    final_qty = variant.inventory_qty if variant.inventory_qty is not None else default_qty
                    
                row["Variant Inventory Qty"] = final_qty
                row["Variant Inventory Policy"] = "deny"
                row["Variant Fulfillment Service"] = "manual"
                
                # 価格計算
                base_price = variant.price
                final_price = int(base_price * markup) if base_price is not None else 0
                row["Variant Price"] = final_price
                
                row["Variant Requires Shipping"] = "true"
                row["Variant Taxable"] = "true" if variant.taxable else "false"
                row["Country of Origin"] = variant.country_of_origin or ""
                row["HS Code"] = variant.hs_code or ""
                
                writer.writerow(row)

            # --- 追加画像 (2枚目以降) ---
            # バリエーション行の消費が終わった後に画像だけの行を追加
            if len(image_urls) > 1:
                for i, img_url in enumerate(image_urls[1:], start=2):
                    writer.writerow({
                        "Handle": handle,
                        "Image Src": img_url,
                        "Image Position": i,
                    })

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_products.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session_db.close()


@app.route("/export_ebay")
def export_ebay():
    """
    eBay File Exchange 用 CSV を出力。
    - ヘッダに UTF-8 指定: Action(SiteID=US|Country=JP|Currency=USD|Version=1193|CC=UTF-8)
    - 価格は (JPY / rate) * markup で USD に変換
    - Brand / Card Condition など最低限の Item Specifics を埋める
    - Business Policies を使う場合は Shipping/Return/Payment のProfile名を入力して利用
    """
    session_db = SessionLocal()
    try:
        products, markup, qty = _parse_ids_and_params(session_db)

        ebay_category_id = request.args.get("ebay_category_id", "").strip()
        ebay_condition_id = request.args.get("ebay_condition_id", "").strip() or "3000"
        paypal_email = request.args.get("ebay_paypal_email", "").strip()
        payment_profile = request.args.get("ebay_payment_profile", "").strip()
        return_profile = request.args.get("ebay_return_profile", "").strip()
        shipping_profile = request.args.get("ebay_shipping_profile", "").strip()

        # ConditionID は数値のみ許可（新品1000 / 中古3000など）
        if not ebay_condition_id.isdigit():
            ebay_condition_id = "3000"

        # 為替レート（JPY -> USD）
        try:
            exchange_rate = float(request.args.get("rate", "155.0"))
        except ValueError:
            exchange_rate = 155.0

        output = io.StringIO()
        writer = csv.writer(output)

        header = [
            "Action(SiteID=US|Country=JP|Currency=USD|Version=1193|CC=UTF-8)",
            "CustomLabel",
            "StartPrice",
            "ConditionID",
            "Title",
            "Description",
            "PicURL",
            "Category",
            "Format",
            "Duration",
            "Location",
            "ShippingProfileName",
            "ReturnProfileName",
            "PaymentProfileName",
            "C:Brand",
            "C:Card Condition",
        ]
        writer.writerow(header)

        BRAND_DEFAULT = "Unbranded"
        CARD_CONDITION_DEFAULT = "Used"

        for p in products:
            snap = p.snapshots[-1] if p.snapshots else None

            # タイトル（80文字制限）
            title_src = snap.title if snap and snap.title else (p.last_title or "")
            title = (title_src or "")[:80]

            # 説明：改行→<br>、セル内改行除去
            description_src = snap.description if snap and snap.description else ""
            if not description_src:
                description_src = title_src
            desc_clean = description_src.replace("\r\n", "\n").replace("\r", "\n")
            description_html = desc_clean.replace("\n", "<br>")

            # 価格計算 (円 -> ドル)
            base_price_yen = None
            if snap and snap.price is not None:
                base_price_yen = snap.price
            elif p.last_price is not None:
                base_price_yen = p.last_price

            start_price = ""
            if base_price_yen:
                try:
                    usd_val = (base_price_yen / exchange_rate) * markup
                    start_price = "{:.2f}".format(usd_val)
                except Exception:
                    start_price = ""

            # 画像 (複数ある場合は | 区切り)
            image_urls = []
            if snap and snap.image_urls:
                image_urls = [u for u in snap.image_urls.split("|") if u]
            pic_url = "|".join(image_urls) if image_urls else ""

            custom_label = f"MERCARI-{p.id}"

            row = [
                "Add",                 # Action
                custom_label,          # CustomLabel
                start_price,           # StartPrice
                ebay_condition_id,     # ConditionID
                title,                 # Title
                description_html,      # Description
                pic_url,               # PicURL
                ebay_category_id,      # Category
                "FixedPriceItem",      # Format
                "GTC",                 # Duration
                "Japan",               # Location
                shipping_profile,      # ShippingProfileName
                return_profile,        # ReturnProfileName
                payment_profile,       # PaymentProfileName
                BRAND_DEFAULT,         # C:Brand
                CARD_CONDITION_DEFAULT # C:Card Condition
            ]
            writer.writerow(row)

        data = "\ufeff" + output.getvalue()
        resp = make_response(data)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = 'attachment; filename="ebay_export.csv"'
        return resp
    finally:
        session_db.close()


@app.route("/export_stock_update")
def export_stock_update():
    """Shopifyの在庫更新用のCSVを生成する"""
    session_db = SessionLocal()
    try:
        product_ids = request.args.getlist("id", type=int)
        
        query = session_db.query(Product)
        
        # ショップフィルタ
        current_shop_id = session.get('current_shop_id')
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)
            
        if product_ids:
            query = query.filter(Product.id.in_(product_ids))
            
        products = query.all()
        if not products:
             return "対象の商品がありません。", 400

        # 在庫数のデフォルト値を取得
        default_qty = request.args.get("qty", "1", type=int)

        output = io.StringIO()
        fieldnames = ["Handle", "Option1 Value", "Option2 Value", "Option3 Value", "Variant Inventory Qty"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            handle = product.custom_handle or f"mercari-{product.id}"
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            
            for variant in variants:
                # 在庫ステータスに応じて在庫数を決定
                if product.last_status == 'sold':
                    final_qty = 0
                else:
                    final_qty = variant.inventory_qty if variant.inventory_qty is not None else default_qty
                
                writer.writerow({
                    "Handle": handle,
                    "Option1 Value": variant.option1_value,
                    "Option2 Value": variant.option2_value,
                    "Option3 Value": variant.option3_value,
                    "Variant Inventory Qty": final_qty,
                })

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_stock_update.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session_db.close()

@app.route("/export_price_update")
def export_price_update():
    """Shopifyの価格更新用のCSVを生成する"""
    session_db = SessionLocal()
    try:
        product_ids = request.args.getlist("id", type=int)
        
        query = session_db.query(Product)
        
        # ショップフィルタ
        current_shop_id = session.get('current_shop_id')
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)
            
        if product_ids:
            query = query.filter(Product.id.in_(product_ids))
            
        products = query.all()
        if not products:
             return "対象の商品がありません。", 400

        # 価格倍率を取得
        markup = request.args.get("markup", "1.0", type=float)

        output = io.StringIO()
        fieldnames = ["Handle", "Option1 Value", "Option2 Value", "Option3 Value", "Variant Price"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            handle = product.custom_handle or f"mercari-{product.id}"
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            
            for variant in variants:
                price = variant.price
                # 価格にマークアップを適用
                final_price = int(price * markup) if price is not None else 0

                writer.writerow({
                    "Handle": handle,
                    "Option1 Value": variant.option1_value,
                    "Option2 Value": variant.option2_value,
                    "Option3 Value": variant.option3_value,
                    "Variant Price": final_price,
                })

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_price_update.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session_db.close()


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
                # 単品スクレイピング実行
                # リストで返ってくるので先頭を取得
                items = scrape_single_item(url, headless=True)
                
                if not items:
                    print(f"  -> Failed to scrape.")
                    continue
                    
                item = items[0]
                new_price = item.get("price")
                new_status = item.get("status") or "unknown"
                new_title = item.get("title") or ""
                
                # 変更検知 (価格 or ステータス)
                # 注意: last_priceがNoneの場合なども考慮
                price_changed = (new_price is not None) and (product.last_price != new_price)
                status_changed = (new_status != "unknown") and (product.last_status != new_status)
                
                if price_changed or status_changed:
                    print(f"  -> CHANGED! Price: {product.last_price}->{new_price}, Status: {product.last_status}->{new_status}")
                    
                    # Product情報の更新 (代表値)
                    product.last_price = new_price
                    product.last_status = new_status
                    product.last_title = new_title 
                    product.updated_at = datetime.utcnow()
                    
                    # Default Title バリエーションの同期
                    default_variant = session_db.query(Variant).filter_by(
                        product_id=product.id, 
                        option1_value="Default Title"
                    ).first()
                    
                    if default_variant:
                        if new_price is not None:
                            default_variant.price = new_price
                        default_variant.inventory_qty = 0 if new_status == 'sold' else (default_variant.inventory_qty or 1)

                    # スナップショット保存
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
                    
                # 連続アクセス負荷軽減
                time.sleep(2)
                
            except Exception as e:
                print(f"  -> Error: {e}")
                import traceback
                traceback.print_exc()
                
        session_db.commit()
        print(f"Finished. Total updated: {updated_count}")
        
    finally:
        session_db.close()


if __name__ == "__main__":
    # ローカル実行時は debug=True
    # Renderでは PORT 環境変数が設定されるので、それを利用
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
