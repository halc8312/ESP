from flask import Flask, render_template, request, make_response, send_from_directory, redirect, url_for, session, has_request_context
from sqlalchemy.orm import subqueryload
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit
import csv
import io
import os
import requests
import shutil
import hashlib

# 独自モジュール
from mercari_db import scrape_search_result, scrape_single_item
from database import SessionLocal, init_db
from models import Shop, Product, Variant, ProductSnapshot, DescriptionTemplate

# ============================== 
# Flask アプリ設定
# ============================== 

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")

# Render/Herokuなどのプロキシ環境下で正しいURLスキーム(https)を取得するための設定
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Flask-Login setup
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

from models import Shop, Product, Variant, ProductSnapshot, DescriptionTemplate, User

@login_manager.user_loader
def load_user(user_id):
    session_db = SessionLocal()
    try:
        return session_db.query(User).get(int(user_id))
    finally:
        session_db.close()

# アプリ起動時にDB初期化（テーブル作成）
with app.app_context():
    init_db()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        session_db = SessionLocal()
        try:
            user = session_db.query(User).filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(url_for('index'))
            else:
                return render_template('login.html', error="Invalid username or password")
        finally:
            session_db.close()
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        session_db = SessionLocal()
        try:
            if session_db.query(User).filter_by(username=username).first():
                return render_template('register.html', error="Username already exists")
            
            new_user = User(username=username)
            new_user.set_password(password)
            session_db.add(new_user)
            session_db.commit()
            
            # Auto login after registration
            login_user(new_user)
            return redirect(url_for('index'))
        except Exception as e:
            return render_template('register.html', error=f"Error: {e}")
        finally:
            session_db.close()
            
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.cli.command("create-user")
def create_user():
    import getpass
    username = input("Username: ")
    password = getpass.getpass("Password: ")
    
    session_db = SessionLocal()
    try:
        if session_db.query(User).filter_by(username=username).first():
            print("User already exists.")
            return

        new_user = User(username=username)
        new_user.set_password(password)
        session_db.add(new_user)
        session_db.commit()
        print(f"User {username} created successfully.")
    except Exception as e:
        print(f"Error: {e}")
        session_db.rollback()
    finally:
        session_db.close()

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

# ============================== 
# 画像保存設定
# ============================== 
IMAGE_STORAGE_PATH = os.environ.get("IMAGE_STORAGE_PATH", os.path.join('static', 'images'))
os.makedirs(IMAGE_STORAGE_PATH, exist_ok=True)

@app.route("/media/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGE_STORAGE_PATH, filename)

def cache_mercari_image(mercari_url, product_id, index):
    if not mercari_url:
        return None
    filename = f"mercari_{product_id}_{index}.jpg"
    local_path = os.path.join(IMAGE_STORAGE_PATH, filename)
    if os.path.exists(local_path):
        return filename
    try:
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

# ============================== 
# ルーティング
# ============================== 

@app.route("/shops", methods=["GET", "POST"])
@login_required
def manage_shops():
    session_db = SessionLocal()
    try:
        if request.method == "POST":
            name = request.form.get("name")
            if name:
                # Check duplication for this user
                existing = session_db.query(Shop).filter_by(user_id=current_user.id, name=name).first()
                if not existing:
                    new_shop = Shop(name=name, user_id=current_user.id)
                    session_db.add(new_shop)
                    try:
                        session_db.commit()
                    except Exception:
                        session_db.rollback()
            return redirect(url_for('manage_shops'))

        # Filter by user
        shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        shop_data = []
        for s in shops:
            count = session_db.query(Product).filter_by(shop_id=s.id).count()
            s.product_count = count
            shop_data.append(s)

        current_shop_id = session.get('current_shop_id')
        
        return render_template(
            "shops.html",
            shops=shop_data,
            all_shops=shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()

@app.route("/shops/<int:shop_id>/delete", methods=["POST"])
@login_required
def delete_shop(shop_id):
    session_db = SessionLocal()
    try:
        # User constraint
        shop = session_db.query(Shop).filter_by(id=shop_id, user_id=current_user.id).one_or_none()
        if shop:
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
@login_required
def set_current_shop():
    shop_id = request.form.get("shop_id")
    # Verify ownership before setting session
    if shop_id:
        session_db = SessionLocal()
        try:
            shop = session_db.query(Shop).filter_by(id=shop_id, user_id=current_user.id).first()
            if shop:
                session['current_shop_id'] = int(shop_id)
        finally:
            session_db.close()
    else:
        session.pop('current_shop_id', None)
    return redirect(request.referrer or url_for('index'))

@app.route("/templates", methods=["GET", "POST"])
@login_required
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
        # Only show user's shops for context if needed, but template is global for now? 
        # Requirement was Shop/Product isolation. Let's filter Shop list in dropdown.
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "manage_templates.html",
            templates=templates,
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()

@app.route("/templates/<int:template_id>/delete", methods=["POST"])
@login_required
def delete_template(template_id):
    session_db = SessionLocal()
    try:
        template = session_db.query(DescriptionTemplate).filter_by(id=template_id).one_or_none()
        if template:
            session_db.delete(template)
            session_db.commit()
    finally:
        session_db.close()
    return redirect(url_for('manage_templates'))

@app.route("/")
@login_required
def index():
    session_db = SessionLocal()
    try:
        page = int(request.args.get("page", 1))
        selected_site = request.args.get("site")
        selected_status = request.args.get("status")
        selected_change_filter = request.args.get("change_filter")

        # Filter query by user_id
        base_query = session_db.query(Product).filter(Product.user_id == current_user.id)

        sites = [s[0] for s in base_query.with_entities(Product.site).distinct().all()]
        statuses = [s[0] for s in base_query.with_entities(Product.last_status).distinct().all()]
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        query = base_query
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)
        if selected_site:
            query = query.filter(Product.site == selected_site)
        if selected_status:
            query = query.filter(Product.last_status == selected_status)

        all_products = query.options(subqueryload(Product.snapshots)).order_by(Product.updated_at.desc()).all()

        products_to_display = []
        for p in all_products:
            p.has_changed = False
            if len(p.snapshots) >= 2:
                sorted_snapshots = sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)
                latest = sorted_snapshots[0]
                previous = sorted_snapshots[1]
                if latest.price != previous.price or latest.status != previous.status:
                    p.has_changed = True
            
            if selected_change_filter == 'changed':
                if p.has_changed:
                    products_to_display.append(p)
            else:
                products_to_display.append(p)

        total_items = len(products_to_display)
        total_pages = (total_items + PAGE_SIZE - 1) // PAGE_SIZE
        offset = (page - 1) * PAGE_SIZE
        paginated_products = products_to_display[offset : offset + PAGE_SIZE]

        has_prev = page > 1
        has_next = page < total_pages

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

        return render_template(
            "index.html",
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
@login_required
def product_detail(product_id):
    session_db = SessionLocal()
    try:
        # User constraint
        product = session_db.query(Product).filter_by(id=product_id, user_id=current_user.id).one_or_none()
        if not product:
            return "Product not found or access denied", 404

        if request.method == "POST":
            # --- 所属ショップ ---
            shop_id_str = request.form.get("shop_id")
            if shop_id_str:
                # Verify shop ownership
                s = session_db.query(Shop).filter_by(id=int(shop_id_str), user_id=current_user.id).first()
                product.shop_id = s.id if s else None
            else:
                product.shop_id = None

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
                        variant.option1_value = request.form.get(f"v_opt1_{v_id}")
                        variant.option2_value = request.form.get(f"v_opt2_{v_id}")
                        
                        p_val = request.form.get(f"v_price_{v_id}")
                        variant.price = int(p_val) if p_val and p_val.isdigit() else None
                        
                        variant.sku = request.form.get(f"v_sku_{v_id}")
                        
                        q_val = request.form.get(f"v_qty_{v_id}")
                        variant.inventory_qty = int(q_val) if q_val and q_val.isdigit() else 0
                        
                        g_val = request.form.get(f"v_grams_{v_id}")
                        variant.grams = int(g_val) if g_val and g_val.isdigit() else None
                        
                        variant.taxable = (request.form.get(f"v_tax_{v_id}") == 'on')
                        variant.hs_code = request.form.get(f"v_hs_{v_id}")
                        variant.country_of_origin = request.form.get(f"v_org_{v_id}")
                except ValueError:
                    continue

            # --- バリエーション新規作成 ---
            new_indices = request.form.getlist("new_v_indices")
            for idx in new_indices:
                try:
                    new_variant = Variant(
                        product_id=product.id,
                        option1_value=request.form.get(f"new_v_opt1_{idx}"),
                        option2_value=request.form.get(f"new_v_opt2_{idx}"),
                        option3_value=request.form.get(f"new_v_opt3_{idx}"),
                        sku=request.form.get(f"new_v_sku_{idx}"),
                        hs_code=request.form.get(f"new_v_hs_{idx}"),
                        country_of_origin=request.form.get(f"new_v_org_{idx}"),
                        taxable=(request.form.get(f"new_v_tax_{idx}") == 'on')
                    )
                    
                    p_val = request.form.get(f"new_v_price_{idx}")
                    if p_val and p_val.isdigit():
                        new_variant.price = int(p_val)
                        
                    q_val = request.form.get(f"new_v_qty_{idx}")
                    if q_val and q_val.isdigit():
                        new_variant.inventory_qty = int(q_val)
                    else:
                        new_variant.inventory_qty = 0
                        
                    g_val = request.form.get(f"new_v_grams_{idx}")
                    if g_val and g_val.isdigit():
                        new_variant.grams = int(g_val)
                    
                    session_db.add(new_variant)
                except Exception as e:
                    print(f"Error adding variant {idx}: {e}")
                    continue

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
            
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        
        variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()

        return render_template(
            "product_detail.html", 
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

@app.route("/scrape", methods=["GET", "POST"])
@login_required
def scrape_form():
    return render_template("scrape_form.html")

@app.route("/scrape/run", methods=["POST"])
@login_required
def scrape_run():
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
        # 単品URLスクレイピング
        items = scrape_single_item(target_url, headless=True)
        new_count, updated_count = save_scraped_items_to_db(items, site="mercari", user_id=current_user.id)
        
    else: # This block handles search results
        params = {}
        if keyword: params["keyword"] = keyword
        if price_min: params["price_min"] = price_min
        if price_max: params["price_max"] = price_max
        if sort: params["sort"] = sort
        if category: params["category_id"] = category

        base = "https://jp.mercari.com/search?"
        query = urlencode(params)
        search_url = base + query

        try:
            items = scrape_search_result(
                search_url=search_url,
                max_items=limit,
                max_scroll=3,
                headless=True,
            )
            new_count, updated_count = save_scraped_items_to_db(items, user_id=current_user.id, site="mercari")
        except Exception as e:
            items = []
            new_count = updated_count = 0
            error_msg = str(e)

    return render_template(
        "scrape_result.html",
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

def _parse_ids_and_params(session_db):
    """
    Export共通のパラメータ解析とProducts取得
    Always filter by current_user.id
    """
    product_ids = request.args.getlist("id", type=int)
    markup = request.args.get("markup", "1.0", type=float)
    qty = request.args.get("qty", "1", type=int)

    query = session_db.query(Product).filter(Product.user_id == current_user.id)
    
    current_shop_id = session.get('current_shop_id')
    if current_shop_id:
        query = query.filter(Product.shop_id == current_shop_id)
        
    if product_ids:
        query = query.filter(Product.id.in_(product_ids))
        
    products = query.all()
    return products, markup, qty

@app.route("/export/shopify")
def export_shopify():
    session_db = SessionLocal()
    try:
        products, markup, default_qty = _parse_ids_and_params(session_db)
        if not products:
             return "対象の商品がありません。", 400
        
        output = io.StringIO()
        fieldnames = [
            "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published", 
            "Option1 Name", "Option1 Value",
            "Option2 Name", "Option2 Value",
            "Option3 Name", "Option3 Value",
            "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
            "Variant Inventory Qty", "Variant Inventory Policy", "Variant Fulfillment Service",
            "Variant Price", "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
            "Variant Barcode", "Image Src", "Image Position", "Image Alt Text",
            "Gift Card", "SEO Title", "SEO Description",
            "Google Shopping / Google Product Category", "Google Shopping / Gender", "Google Shopping / Age Group",
            "Google Shopping / MPN", "Google Shopping / AdWords Grouping", "Google Shopping / AdWords Labels",
            "Google Shopping / Condition", "Google Shopping / Custom Product", "Google Shopping / Custom Label 0",
            "Google Shopping / Custom Label 1", "Google Shopping / Custom Label 2", "Google Shopping / Custom Label 3",
            "Google Shopping / Custom Label 4", "Variant Image", "Variant Weight Unit", "Variant Tax Code",
            "Cost per item", "Status"
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

            title = product.custom_title or product.last_title or ""
            # Description processing remains same
            description = product.custom_description or (snapshot.description if snapshot else "")
            vendor = product.custom_vendor or product.site.capitalize()
            handle = product.custom_handle or f"mercari-{product.id}"
            
            image_urls = []
            if snapshot and snapshot.image_urls:
                base_url = request.url_root.rstrip('/')
                original_urls = snapshot.image_urls.split("|")
                for i, mercari_url in enumerate(original_urls):
                    local_filename = cache_mercari_image(mercari_url, product.id, i)
                    if local_filename:
                        full_url = f"{base_url}/media/{local_filename}"
                        image_urls.append(full_url)
            
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            if not variants:
                continue

            for i, variant in enumerate(variants):
                row = {f: "" for f in fieldnames} # Initialize with empty strings
                row["Handle"] = handle
                
                # Common fields (Status needed for all rows per feedback, though standard is 1st row. We will put in all if safe, or follow standard strictly. Feedback says "most rows empty" is bad. Let's put Status in all rows to be safe as per feedback.)
                row["Status"] = product.status

                if i == 0:
                    row["Title"] = title
                    row["Body (HTML)"] = description.replace("\n", "<br>")
                    row["Vendor"] = vendor
                    row["Type"] = "Mercari Item" # Default Type
                    row["Published"] = "true" if product.status == 'active' else 'false'
                    row["Tags"] = product.tags or ""
                    row["SEO Title"] = product.seo_title or ""
                    row["SEO Description"] = product.seo_description or ""
                    if image_urls:
                        row["Image Src"] = image_urls[0]
                        row["Image Position"] = 1
                        row["Image Alt Text"] = title
                
                row["Option1 Name"] = product.option1_name or "Title"
                row["Option2 Name"] = product.option2_name or ""
                row["Option3 Name"] = product.option3_name or ""
                
                row["Option1 Value"] = variant.option1_value
                row["Option2 Value"] = variant.option2_value
                row["Option3 Value"] = variant.option3_value
                row["Variant SKU"] = variant.sku or ""
                row["Variant Grams"] = variant.grams or ""
                row["Variant Inventory Tracker"] = "shopify"
                
                if product.last_status == 'sold':
                    final_qty = 0
                else:
                    final_qty = variant.inventory_qty if variant.inventory_qty is not None else default_qty
                    
                row["Variant Inventory Qty"] = final_qty
                row["Variant Inventory Policy"] = "deny"
                row["Variant Fulfillment Service"] = "manual"
                
                base_price = variant.price
                final_price = int(base_price * markup) if base_price is not None else 0
                row["Variant Price"] = final_price
                row["Variant Compare At Price"] = "" # Empty for now
                
                row["Variant Requires Shipping"] = "true"
                row["Variant Taxable"] = "true" if variant.taxable else "false"
                # Removed Country of Origin and HS Code per feedback
                row["Variant Barcode"] = "" # Empty
                
                writer.writerow(row)

            if len(image_urls) > 1:
                for i, img_url in enumerate(image_urls[1:], start=2):
                    img_row = {f: "" for f in fieldnames}
                    img_row["Handle"] = handle
                    img_row["Image Src"] = img_url
                    img_row["Image Position"] = i
                    img_row["Image Alt Text"] = title
                    writer.writerow(img_row)

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_products.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session_db.close()

@app.route("/export_ebay")
def export_ebay():
    session_db = SessionLocal()
    try:
        products, markup, qty = _parse_ids_and_params(session_db)
        ebay_category_id = request.args.get("ebay_category_id", "").strip()
        ebay_condition_id = request.args.get("ebay_condition_id", "").strip() or "3000"
        paypal_email = request.args.get("ebay_paypal_email", "").strip()
        payment_profile = request.args.get("ebay_payment_profile", "").strip()
        return_profile = request.args.get("ebay_return_profile", "").strip()
        shipping_profile = request.args.get("ebay_shipping_profile", "").strip()

        if not ebay_condition_id.isdigit():
            ebay_condition_id = "3000"

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
            title_src = snap.title if snap and snap.title else (p.last_title or "")
            title = (title_src or "")[:80]
            description_src = snap.description if snap and snap.description else ""
            if not description_src:
                description_src = title_src
            desc_clean = description_src.replace("\r\n", "\n").replace("\r", "\n")
            description_html = desc_clean.replace("\n", "<br>")

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

            image_urls = []
            if snap and snap.image_urls:
                image_urls = [u for u in snap.image_urls.split("|") if u]
            pic_url = "|".join(image_urls) if image_urls else ""

            custom_label = f"MERCARI-{p.id}"

            row = [
                "Add", custom_label, start_price, ebay_condition_id, title, description_html, pic_url,
                ebay_category_id, "FixedPriceItem", "GTC", "Japan", shipping_profile, return_profile,
                payment_profile, BRAND_DEFAULT, CARD_CONDITION_DEFAULT
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
    session_db = SessionLocal()
    try:
        products, markup, default_qty = _parse_ids_and_params(session_db)
        if not products:
             return "対象の商品がありません。", 400

        output = io.StringIO()
        fieldnames = ["Handle", "Option1 Value", "Option2 Value", "Option3 Value", "Variant Inventory Qty"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            handle = product.custom_handle or f"mercari-{product.id}"
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            
            for variant in variants:
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
    session_db = SessionLocal()
    try:
        products, markup, default_qty = _parse_ids_and_params(session_db)
        if not products:
             return "対象の商品がありません。", 400

        output = io.StringIO()
        fieldnames = ["Handle", "Option1 Value", "Option2 Value", "Option3 Value", "Variant Price"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            handle = product.custom_handle or f"mercari-{product.id}"
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            
            for variant in variants:
                price = variant.price
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
                import traceback
                traceback.print_exc()
                
        session_db.commit()
        print(f"Finished. Total updated: {updated_count}")
        
    finally:
        session_db.close()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))