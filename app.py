from flask import Flask, render_template_string, request, make_response, send_from_directory
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit  # URLパラメータ & 正規化用
import csv
import io
import os
import requests
import shutil

# mercari_db.py からスクレイピング関数を import
from mercari_db import scrape_search_result

# ==============================
# SQLAlchemy モデル定義
# ==============================

Base = declarative_base()


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    site = Column(String, nullable=False, index=True)
    source_url = Column(String, nullable=False, unique=True, index=True)

    last_title = Column(String)
    last_price = Column(Integer)
    last_status = Column(String)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    snapshots = relationship("ProductSnapshot", back_populates="product")


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


# ==============================
# DB 接続設定（WAL 有効）
# ==============================

engine = create_engine("sqlite:///mercari.db", echo=False)
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
    session = SessionLocal()
    now = datetime.utcnow()
    new_count = 0
    updated_count = 0

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

            # 既存の Product を検索
            product = session.query(Product).filter_by(source_url=url).one_or_none()

            if product is None:
                # 新規作成
                product = Product(
                    site=site,
                    source_url=url,
                    last_title=title,
                    last_price=price,
                    last_status=status,
                    created_at=now,
                    updated_at=now,
                )
                session.add(product)
                session.flush()  # ID 発行
                new_count += 1
            else:
                # 更新
                product.last_title = title
                product.last_price = price
                product.last_status = status
                product.updated_at = now
                updated_count += 1

            snapshot = ProductSnapshot(
                product_id=product.id,
                scraped_at=now,
                title=title,
                price=price,
                status=status,
                description=description,
                image_urls=image_urls_str,
            )
            session.add(snapshot)

        session.commit()
        return new_count, updated_count
    except Exception as e:
        session.rollback()
        print("DB 保存エラー:", e)
        return 0, 0
    finally:
        session.close()


# ==============================
# Flask アプリ
# ==============================

app = Flask(__name__)

# ==============================
# 画像保存設定
# ==============================
# staticフォルダの中に images フォルダを作る
IMAGE_FOLDER = os.path.join('static', 'images')
os.makedirs(IMAGE_FOLDER, exist_ok=True)


def cache_mercari_image(mercari_url, product_id, index):
    """
    メルカリの画像をダウンロードし、ローカルのファイル名を返す。
    失敗した場合は None を返す。
    """
    if not mercari_url:
        return None
        
    # ファイル名: mercari_{ID}_{連番}.jpg
    filename = f"mercari_{product_id}_{index}.jpg"
    local_path = os.path.join(IMAGE_FOLDER, filename)
    
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
        .nav { margin-bottom: 10px; }
        .nav a { margin-right: 15px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="nav">
        <a href="{{ url_for('index') }}">商品一覧</a>
        <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
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
            <button type="submit">フィルタ</button>
        </div>
    </form>

    <!-- エクスポート用フォーム -->
    <form method="GET">
        <div class="actions">
            <div>
                <label>価格倍率 (markup)</label>
                <input type="number" name="markup" value="{{ default_markup }}" step="0.01">
                <span style="font-size: 11px; color: #555;">1.0=そのまま / 1.2=20%上乗せ</span>
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
                {% set snap = p.snapshots[-1] if p.snapshots else None %}
                {% set thumb_url = None %}
                {% set image_count = 0 %}
                {% if snap and snap.image_urls %}
                    {% set urls = snap.image_urls.split('|') %}
                    {% set image_count = urls|length %}
                    {% if urls and urls[0] %}
                        {% set thumb_url = urls[0] %}
                    {% endif %}
                {% endif %}
            <tr>
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
                <td><a href="{{ url_for('product_detail', product_id=p.id) }}">詳細</a></td>
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
</body>
</html>
"""

# 詳細テンプレート
DETAIL_TEMPLATE = """
<!doctype html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <title>商品詳細 - {{ product.last_title }}</title>
    <style>
        body { font-family: sans-serif; max-width: 900px; margin: 0 auto; }
        .meta { margin-bottom: 16px; }
        .meta dt { font-weight: bold; }
        .meta dd { margin: 0 0 8px 0; }
        .images { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
        .images img { max-width: 200px; max-height: 200px; object-fit: contain; border: 1px solid #ccc; padding: 4px; background: #fafafa; }
        pre.description { white-space: pre-wrap; background: #f8f8f8; padding: 8px; border: 1px solid #ddd; }
        a { color: #06c; }
    </style>
</head>
<body>
    <h1>商品詳細</h1>
    <p><a href="{{ url_for('index') }}">&laquo; 一覧に戻る</a></p>

    <dl class="meta">
        <dt>ID</dt>
        <dd>{{ product.id }}</dd>

        <dt>サイト</dt>
        <dd>{{ product.site }}</dd>

        <dt>商品名</dt>
        <dd>{{ product.last_title }}</dd>

        <dt>価格</dt>
        <dd>
            {% if snapshot and snapshot.price is not none %}
                ¥{{ "{:,}".format(snapshot.price) }}
            {% elif product.last_price is not none %}
                ¥{{ "{:,}".format(product.last_price) }}
            {% else %}
                -
            {% endif %}
        </dd>

        <dt>ステータス</dt>
        <dd>{{ snapshot.status if snapshot else product.last_status }}</dd>

        <dt>元URL</dt>
        <dd><a href="{{ product.source_url }}" target="_blank">{{ product.source_url }}</a></dd>

        <dt>最終スクレイピング日時</dt>
        <dd>{{ snapshot.scraped_at if snapshot else product.updated_at }}</dd>
    </dl>

    <h2>商品説明</h2>
    {% if snapshot and snapshot.description %}
        <pre class="description">{{ snapshot.description }}</pre>
    {% else %}
        <p>(説明なし)</p>
    {% endif %}

    <h2>画像（{{ images|length }}枚）</h2>
    {% if images %}
        <div class="images">
            {% for url in images %}
                <a href="{{ url }}" target="_blank">
                    <img src="{{ url }}" alt="image {{ loop.index }}">
                </a>
            {% for %}
        </div>
    {% else %}
        <p>(画像なし)</p>
    {% endif %}
</body>
</html>
"""


@app.route("/")
def index():
    session = SessionLocal()
    try:
        selected_site = request.args.get("site", "").strip() or ""
        selected_status = request.args.get("status", "").strip() or ""
        page_str = request.args.get("page", "1")

        try:
            page = int(page_str)
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        query = session.query(Product)
        if selected_site:
            query = query.filter(Product.site == selected_site)
        if selected_status:
            query = query.filter(Product.last_status == selected_status)

        total_count = query.count()
        total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        if page > total_pages:
            page = total_pages

        products = (
            query.order_by(Product.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        site_rows = session.query(Product.site).distinct().all()
        status_rows = session.query(Product.last_status).distinct().all()
        sites = sorted({row[0] for row in site_rows if row[0]})
        statuses = sorted({row[0] for row in status_rows if row[0]})

        has_prev = page > 1
        has_next = page < total_pages

        return render_template_string(
            INDEX_TEMPLATE,
            products=products,
            sites=sites,
            statuses=statuses,
            selected_site=selected_site,
            selected_status=selected_status,
            default_markup=1.0,
            default_qty=1,
            default_rate=155.0,          # デフォルト為替レート
            default_ebay_category_id="",
            default_ebay_condition_id="3000",          # デフォルト: 中古
            default_ebay_payment_profile="",
            default_ebay_return_profile="",
            default_ebay_shipping_profile="",
            default_ebay_paypal_email="",
            page=page,
            total_pages=total_pages,
            has_prev=has_prev,
            has_next=has_next,
        )
    finally:
        session.close()


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    session = SessionLocal()
    try:
        product = session.query(Product).get(product_id)
        if not product:
            return "Not found", 404

        snapshot = product.snapshots[-1] if product.snapshots else None
        images = []
        if snapshot and snapshot.image_urls:
            images = [u for u in snapshot.image_urls.split("|") if u]

        return render_template_string(
            DETAIL_TEMPLATE,
            product=product,
            snapshot=snapshot,
            images=images,
        )
    finally:
        session.close()


# =========================================================
# スクレイピング設定フォーム
# =========================================================

@app.route("/scrape", methods=["GET"])
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
    keyword = request.form.get("keyword", "")
    price_min = request.form.get("price_min")
    price_max = request.form.get("price_max")
    sort = request.form.get("sort", "created_desc")
    category = request.form.get("category")
    limit = int(request.form.get("limit", 10))

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
        # headless=False にするとブラウザが見える（True にすると裏で動く）
        items = scrape_search_result(
            search_url=search_url,
            max_items=limit,
            max_scroll=3,
            headless=False,
        )
        new_count, updated_count = save_scraped_items_to_db(items, site="mercari")
        error_msg = ""
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

def _parse_ids_and_params(session):
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

    query = session.query(Product)
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
    """
    Shopify 用 CSV 出力（修正版）
    - JPYをそのまま出力（ドル換算なし）
    - 全行に必須項目（Price, Inventory quantity等）を埋める
    - メルカリの画像をダウンロードして自サーバーのURLに置き換える
    """
    session = SessionLocal()
    try:
        products, markup, qty = _parse_ids_and_params(session)
        # ※為替レート(rate)の取得は削除します

        # 現在のアプリのURLルートを取得（例: https://my-app.onrender.com）
        # request.url_root は末尾に / がつくので調整
        base_url = request.url_root.rstrip('/')

        output = io.StringIO()
        writer = csv.writer(output)

        header = [
            "Title", "URL handle", "Description", "Vendor", "Product category",
            "Type", "Tags", "Published on online store", "Status", "SKU", "Barcode",
            "Option1 name", "Option1 value", "Option2 name", "Option2 value", "Option3 name", "Option3 value",
            "Price", "Compare-at price", "Cost per item", "Charge tax", "Tax code",
            "Unit price total measure", "Unit price total measure unit", "Unit price base measure", "Unit price base measure unit",
            "Inventory tracker", "Inventory quantity", "Continue selling when out of stock",
            "Weight value (grams)", "Weight unit for display", "Requires shipping", "Fulfillment service",
            "Product image URL", "Image position", "Image alt text", "Variant image URL",
            "Gift card", "SEO title", "SEO description"
        ]
        writer.writerow(header)

        for p in products:
            snap = p.snapshots[-1] if p.snapshots else None

            # タイトル・説明
            title = snap.title if snap and snap.title else (p.last_title or "")
            description = snap.description if snap and snap.description else ""
            desc_clean = description.replace("\r\n", "\n").replace("\r", "\n")
            desc_html = desc_clean.replace("\n", "<br>")

            # ★修正1: 価格は日本円のまま出力（マークアップのみ適用）
            price_val = ""
            base_price = None
            if snap and snap.price is not None:
                base_price = snap.price
            elif p.last_price is not None:
                base_price = p.last_price
            
            if base_price:
                # 単純に円 × マークアップ
                val = int(base_price * markup)
                price_val = str(val)

            # ★ 画像処理の変更箇所 ★
            original_image_urls = []
            if snap and snap.image_urls:
                original_image_urls = [u for u in snap.image_urls.split("|") if u]

            handle = f"mercari-{p.id}"
            sku = f"MER-{p.id}"

            loop_count = max(len(original_image_urls), 1)
            for i in range(loop_count):
                original_url = original_image_urls[i] if i < len(original_image_urls) else ""
                
                # ここで画像をダウンロードし、自分のサーバーのURLに変換する
                my_server_image_url = ""
                if original_url:
                    cached_filename = cache_mercari_image(original_url, p.id, i)
                    if cached_filename:
                        # 変換後: https://my-app.onrender.com/static/images/mercari_123_0.jpg
                        my_server_image_url = f"{base_url}/static/images/{cached_filename}"
                    else:
                        # ダウンロード失敗時は元のURLを入れておく（ダメ元で）
                        my_server_image_url = original_url

                is_first = (i == 0)

                # 1行目だけに入れるべき情報
                row_title = title if is_first else ""
                row_body = desc_html if is_first else ""
                row_tags = "Imported" if is_first else ""

                # ★修正2: 全行に入れるべき情報（エラー回避のため）
                # これにより、どの行が読み込まれても「この商品は在庫1、価格X円、手動配送」と認識させます
                row_handle = handle
                row_option1_name = "Title"
                row_option1_value = "Default Title"
                row_price = price_val  # 全行に価格を入れる
                row_inventory_tracker = "shopify"
                row_inventory_qty = qty # 全行に在庫数を入れる
                row_continue_selling = "deny"
                row_sku = sku
                row_published = "TRUE"
                row_status = "active"
                row_requires_shipping = "TRUE"
                row_fulfillment = "manual"
                row_taxable = "FALSE"
                row_weight_val = "0"
                row_weight_unit = "g"
                row_gift_card = "FALSE"

                row = [
                    row_title,              # Title (1行目のみ)
                    row_handle,             # URL handle
                    row_body,               # Description (1行目のみ)
                    "Mercari",              # Vendor
                    "",                     # Product category
                    "",                     # Type
                    row_tags,               # Tags
                    row_published,          # Published
                    row_status,             # Status
                    row_sku,                # SKU
                    "",                     # Barcode
                    row_option1_name,       # Option1 name
                    row_option1_value,      # Option1 value
                    "", "", "", "",         # Option2, 3
                    row_price,              # Price (★全行)
                    "",                     # Compare-at
                    "",                     # Cost per item
                    row_taxable,            # Charge tax
                    "",                     # Tax code
                    "", "", "", "",         # Unit prices
                    row_inventory_tracker,  # Inventory tracker
                    row_inventory_qty,      # Inventory quantity (★全行)
                    row_continue_selling,   # Continue selling
                    row_weight_val,         # Weight value
                    row_weight_unit,        # Weight unit
                    row_requires_shipping,  # Requires shipping
                    row_fulfillment,        # Fulfillment service
                    my_server_image_url,    # Product image URL (★修正: 自サーバーのURLに置き換え)
                    i + 1 if my_server_image_url else "", # Image position
                    "",                     # Image alt text
                    "",                     # Variant image URL
                    row_gift_card,          # Gift card
                    "", "",                 # SEO
                ]
                writer.writerow(row)

        data = "\ufeff" + output.getvalue()
        resp = make_response(data)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = 'attachment; filename="shopify_export_fixed.csv"'
        return resp
    except Exception as e:
        print(e)
        return str(e), 500
    finally:
        session.close()


@app.route("/export/ebay")
def export_ebay():
    """
    eBay File Exchange 用 CSV を出力。
    - ヘッダに UTF-8 指定: Action(SiteID=US|Country=JP|Currency=USD|Version=1193|CC=UTF-8)
    - 価格は (JPY / rate) * markup で USD に変換
    - Brand / Card Condition など最低限の Item Specifics を埋める
    - Business Policies を使う場合は Shipping/Return/Payment のProfile名を入力して利用
    """
    session = SessionLocal()
    try:
        products, markup, qty = _parse_ids_and_params(session)

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
        session.close()


if __name__ == "__main__":
    # ローカル実行時は debug=True
    # Renderでは PORT 環境変数が設定されるので、それを利用
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
