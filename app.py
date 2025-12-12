from flask import Flask, render_template_string, request, make_response, send_from_directory, redirect, url_for
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
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, subqueryload
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

    # ユーザーによる編集内容を保存するカラム
    custom_title = Column(String)
    custom_price = Column(Integer)
    custom_description = Column(Text)

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
# テンプレート管理
# ==============================

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
        .nav { margin-bottom: 10px; }
        .nav a { margin-right: 15px; font-weight: bold; }
        .template-form {
            border: 1px solid #ccc;
            padding: 16px;
            margin-top: 16px;
        }
    </style>
</head>
<body>
    <div class="nav">
        <a href="{{ url_for('index') }}">商品一覧</a>
        <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
        <a href="{{ url_for('manage_templates') }}">テンプレート管理</a>
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

@app.route("/templates", methods=["GET", "POST"])
def manage_templates():
    session = SessionLocal()
    try:
        if request.method == "POST":
            name = request.form.get("name")
            content = request.form.get("content")
            if name and content:
                new_template = DescriptionTemplate(name=name, content=content)
                session.add(new_template)
                session.commit()
            return redirect(url_for('manage_templates'))

        templates = session.query(DescriptionTemplate).order_by(DescriptionTemplate.id).all()
        return render_template_string(TEMPLATES_TEMPLATE, templates=templates)
    finally:
        session.close()

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
        .nav { margin-bottom: 10px; }
        .nav a { margin-right: 15px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="nav">
        <a href="{{ url_for('index') }}">商品一覧</a>
        <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
        <a href="{{ url_for('manage_templates') }}">テンプレート管理</a>
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
        body { font-family: sans-serif; max-width: 900px; margin: 0 auto; }
        .nav { margin-bottom: 10px; }
        .nav a { margin-right: 15px; font-weight: bold; }
        .form-group { margin-bottom: 12px; }
        .form-group label { display: block; font-weight: bold; margin-bottom: 4px; }
        .form-group input, .form-group textarea, .form-group select {
            width: 100%;
            box-sizing: border-box;
            padding: 4px;
        }
        .form-group textarea { min-height: 200px; }
        .original-info {
            font-size: 12px;
            color: #555;
            background: #f4f4f4;
            padding: 8px;
            border: 1px solid #ddd;
            margin-top: 4px;
        }
        .images { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
        .images img { max-width: 150px; max-height: 150px; border: 1px solid #ccc; }
    </style>
</head>
<body>
    <div class="nav">
        <a href="{{ url_for('index') }}">商品一覧</a>
        <a href="{{ url_for('scrape_form') }}">新規スクレイピング</a>
        <a href="{{ url_for('manage_templates') }}">テンプレート管理</a>
    </div>

    <h1>商品編集</h1>
    <p><a href="{{ url_for('index') }}">&laquo; 一覧に戻る</a></p>

    <form method="POST">
        <div class="form-group">
            <label for="title">商品名</label>
            <input type="text" id="title" name="title" value="{{ product.custom_title or product.last_title or '' }}">
            <div class="original-info">
                <strong>元の名前:</strong> {{ product.last_title or '(なし)' }}
            </div>
        </div>

        <div class="form-group">
            <label for="price">価格</label>
            <input type="number" id="price" name="price" value="{{ product.custom_price or product.last_price or '' }}">
            <div class="original-info">
                <strong>元の価格:</strong> ¥{{ "{:,}".format(product.last_price) if product.last_price is not none else '(なし)' }}
            </div>
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
            <label for="description">商品説明</label>
            <textarea id="description" name="description">{{ product.custom_description or (snapshot.description if snapshot else '') }}</textarea>
            <div class="original-info">
                <strong>元の説明(抜粋):</strong><br>
                {{ (snapshot.description[:200] + '...') if snapshot and snapshot.description else '(なし)' }}
            </div>
        </div>

        <button type="submit">保存</button>
    </form>

    <h2>画像（{{ images|length }}枚）</h2>
    {% if images %}
        <div class="images">
            {% for url in images %}
                <img src="{{ url }}" alt="image {{ loop.index }}">
            {% endfor %}
        </div>
    {% else %}
        <p>(画像なし)</p>
    {% endif %}

    <script>
        function applyTemplate() {
            var select = document.getElementById('template');
            var content = select.value;
            if (content) {
                document.getElementById('description').value = content;
            }
        }
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    session = SessionLocal()
    try:
        page = int(request.args.get("page", 1))

        # フィルタリング条件の取得
        selected_site = request.args.get("site")
        selected_status = request.args.get("status")
        selected_change_filter = request.args.get("change_filter")

        # サイトとステータスのリストを取得
        sites = [s[0] for s in session.query(Product.site).distinct().all()]
        statuses = [s[0] for s in session.query(Product.last_status).distinct().all()]

        # 基本的なクエリ
        query = session.query(Product)
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
        )
    finally:
        session.close()


@app.route("/product/<int:product_id>", methods=["GET", "POST"])
def product_detail(product_id):
    session = SessionLocal()
    try:
        product = session.query(Product).filter_by(id=product_id).one_or_none()
        if not product:
            return "Product not found", 404

        if request.method == "POST":
            # フォームから送信されたデータで product を更新
            product.custom_title = request.form.get("title")
            
            price_str = request.form.get("price")
            product.custom_price = int(price_str) if price_str.isdigit() else None
            
            product.custom_description = request.form.get("description")
            product.updated_at = datetime.utcnow()
            
            session.commit()
            return redirect(url_for('product_detail', product_id=product.id))

        snapshot = (
            session.query(ProductSnapshot)
            .filter_by(product_id=product.id)
            .order_by(ProductSnapshot.scraped_at.desc())
            .first()
        )
        
        templates = session.query(DescriptionTemplate).order_by(DescriptionTemplate.name).all()

        images = []
        if snapshot and snapshot.image_urls:
            images = snapshot.image_urls.split("|")

        return render_template_string(
            DETAIL_TEMPLATE, product=product, snapshot=snapshot, images=images, templates=templates
        )
    finally:
        session.close()


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
        # headless=True にして、サーバー環境でGUIなしでブラウザを動かす
        items = scrape_search_result(
            search_url=search_url,
            max_items=limit,
            max_scroll=3,
            headless=True,
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
    Shopify 用 CSV 出力（仕様準拠版）
    - 1行目に商品情報と1枚目の画像を記載
    - 2行目以降は Handle と画像URLのみを記載して画像を追加
    """
    session = SessionLocal()
    try:
        products, markup, qty = _parse_ids_and_params(session)
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
        
        # ヘッダーのインデックスを事前に取得しておくと便利
        handle_idx = header.index("URL handle")
        img_url_idx = header.index("Product image URL")
        img_pos_idx = header.index("Image position")

        for p in products:
            snap = p.snapshots[-1] if p.snapshots else None

            title = snap.title if snap and snap.title else (p.last_title or "")
            description = snap.description if snap and snap.description else ""
            desc_html = description.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")

            price_val = ""
            base_price = snap.price if snap and snap.price is not None else p.last_price
            if base_price:
                price_val = str(int(base_price * markup))

            handle = f"mercari-{p.id}"
            sku = f"MER-{p.id}"

            # 画像URLを自サーバーのURLに変換
            my_server_image_urls = []
            if snap and snap.image_urls:
                original_image_urls = [u for u in snap.image_urls.split("|") if u]
                for i, original_url in enumerate(original_image_urls):
                    cached_filename = cache_mercari_image(original_url, p.id, i)
                    if cached_filename:
                        my_server_image_urls.append(f"{base_url}/media/{cached_filename}")

            # --- 1行目（商品本体）のデータを作成 ---
            first_image_url = my_server_image_urls[0] if my_server_image_urls else ""
            row = [
                title, handle, desc_html, "Mercari", "", "", "Imported", 
                "TRUE", "active", sku, "", "Title", "Default Title", 
                "", "", "", "", price_val, "", "", "FALSE", "", "", "", "", "", 
                "shopify", qty, "deny", "0", "g", "TRUE", "manual",
                first_image_url, "1" if first_image_url else "", "", "", "FALSE", "", ""
            ]
            writer.writerow(row)

            # --- 2行目以降（追加画像）のデータを作成 ---
            if len(my_server_image_urls) > 1:
                for i in range(1, len(my_server_image_urls)):
                    # 空の行を作成
                    additional_image_row = [""] * len(header)
                    # Handle と Image URL, Position のみ設定
                    additional_image_row[handle_idx] = handle
                    additional_image_row[img_url_idx] = my_server_image_urls[i]
                    additional_image_row[img_pos_idx] = i + 1
                    writer.writerow(additional_image_row)

        data = "\ufeff" + output.getvalue()
        resp = make_response(data)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = 'attachment; filename="shopify_export_final.csv"'
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


@app.route("/export_stock_update")
def export_stock_update():
    """Shopifyの在庫更新用のCSVを生成する"""
    session = SessionLocal()
    try:
        product_ids = request.args.getlist("id", type=int)
        if not product_ids:
            return "商品が選択されていません。", 400

        # 在庫数のデフォルト値を取得
        default_qty = request.args.get("qty", "1", type=int)

        products = session.query(Product).filter(Product.id.in_(product_ids)).all()

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["Handle", "Variant Inventory Qty"])
        writer.writeheader()

        for product in products:
            # 在庫ステータスに応じて在庫数を決定
            # 'sold' なら 0、そうでなければ指定された在庫数
            inventory_qty = 0 if product.last_status == 'sold' else default_qty
            
            writer.writerow({
                "Handle": f"mercari-{product.id}",
                "Variant Inventory Qty": inventory_qty,
            })

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_stock_update.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session.close()

@app.route("/export_price_update")
def export_price_update():
    """Shopifyの価格更新用のCSVを生成する"""
    session = SessionLocal()
    try:
        product_ids = request.args.getlist("id", type=int)
        if not product_ids:
            return "商品が選択されていません。", 400

        # 価格倍率を取得
        markup = request.args.get("markup", "1.0", type=float)

        products = session.query(Product).filter(Product.id.in_(product_ids)).all()

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["Handle", "Variant Price"])
        writer.writeheader()

        for product in products:
            # 編集後の価格(custom_price)を優先し、なければ元の価格(last_price)を使用
            price = product.custom_price if product.custom_price is not None else product.last_price
            
            # 価格にマークアップを適用
            final_price = int(price * markup) if price is not None else 0

            writer.writerow({
                "Handle": f"mercari-{product.id}",
                "Variant Price": final_price,
            })

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_price_update.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session.close()


if __name__ == "__main__":
    # ローカル実行時は debug=True
    # Renderでは PORT 環境変数が設定されるので、それを利用
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
