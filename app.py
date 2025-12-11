from flask import Flask, render_template_string, request, make_response, send_from_directory, redirect, url_for, session
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    Boolean,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit  # URLパラメータ & 正規化用
import time
import csv
import io
import os
import requests
import shutil
import smtplib
from email.mime.text import MIMEText
from email.header import Header

# mercari_db.py からスクレイピング関数を import
from scrapers import scrape_search_result

# ==============================
# SQLAlchemy モデル定義
# ==============================

Base = declarative_base()


class Shop(Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    platform = Column(String, default="shopify") # "shopify", "ebay"
    
    # Shopify-specific
    api_key = Column(String)
    api_secret = Column(String)
    shop_url = Column(String) # e.g., "your-store.myshopify.com"

    created_at = Column(DateTime, default=datetime.utcnow)
    products = relationship("Product", back_populates="shop")

class Template(Base):
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    content_html = Column(Text)
    content_text = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    
    shop_id = Column(Integer, ForeignKey("shops.id"))

    site = Column(String, nullable=False, index=True)
    source_url = Column(String, nullable=False, unique=True, index=True)

    last_title = Column(String)
    last_price = Column(Integer)
    last_status = Column(String)

    # --- Editable fields for Shopify/eBay ---
    edited_title = Column(String)
    edited_price = Column(Integer)
    edited_description = Column(Text)
    tags = Column(String) # comma-separated
    
    # --- Change Tracking ---
    last_checked_at = Column(DateTime)
    has_changed = Column(Boolean, default=False)
    # ----------------------------------------

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    shop = relationship("Shop", back_populates="products")
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

# Renderの永続ディスクを利用する場合、そのパスを環境変数で指定できるようにします。
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


def save_scraped_items_to_db(items, site: str = "mercari", shop_id: int = None):
    """
    mercari_db.scrape_search_result() が返した items(list[dict]) を
    Product / ProductSnapshot に保存する。
    """
    session = SessionLocal()
    now = datetime.utcnow()
    new_count = 0
    updated_count = 0
    
    if shop_id is None:
        print("Warning: shop_id is not provided to save_scraped_items_to_db")
        # フォールバックとして最初のショップを選ぶなどもあり得る
        shop = session.query(Shop).first()
        if not shop:
             print("Error: No shops found in DB. Cannot save products.")
             return 0, 0
        shop_id = shop.id

    try:
        for item in items:
            raw_url = item.get("url", "")
            if not raw_url:
                continue

            url = normalize_url(raw_url)

            # 既存の Product を shop_id と URL で検索
            product = session.query(Product).filter_by(source_url=url, shop_id=shop_id).one_or_none()

            title = item.get("title") or ""
            price = item.get("price")
            status = item.get("status") or ""
            description = item.get("description") or ""
            image_urls = item.get("image_urls") or []
            image_urls_str = "|".join(image_urls)


            if product is None:
                # 新規作成
                product = Product(
                    shop_id=shop_id,
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
# サポート機能
# ==============================
def send_support_email(subject, from_email, message):
    """サポートリクエストをメールで送信する"""
    to_email = os.environ.get("SUPPORT_EMAIL_TO") # 送信先 (中川様)
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")

    if not all([to_email, smtp_host, smtp_port, smtp_user, smtp_pass]):
        print("ERROR: SMTP environment variables are not fully configured.")
        return False

    try:
        body = f"返信先: {from_email or '指定なし'}\\n\\n---\\n\\n{message}"
        
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = smtp_user
        msg['To'] = to_email

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False

@app.route("/support", methods=["GET", "POST"])
def support():
    if request.method == "POST":
        subject = request.form.get("subject")
        message = request.form.get("message")
        from_email = request.form.get("from_email")

        success = send_support_email(subject, from_email, message)

        if success:
            # flash("メッセージを送信しました。")
            pass
        else:
            # flash("メッセージの送信に失敗しました。管理者にご確認ください。", "danger")
            pass

        return redirect(url_for('support'))

    # 他のテンプレートと同様のナビゲーションバーを持つHTML
    html = """
    <!doctype html>
    <html lang="ja">
    <head>
        <meta charset="utf-8">
        <title>サポート</title>
        <link href="{{ url_for('static', filename='bootstrap/css/bootstrap.min.css') }}" rel="stylesheet">
    </head>
    <body class="p-3">
        <div class="container">
             <nav class="navbar navbar-expand-lg navbar-light bg-light mb-3">
                <div class="container-fluid">
                    <a class="navbar-brand" href="/">E-Com Tool</a>
                    <div class="collapse navbar-collapse">
                        <ul class="navbar-nav me-auto mb-2 mb-lg-0">
                            <li class="nav-item"><a class="nav-link" href="{{ url_for('index') }}">商品一覧</a></li>
                            <li class="nav-item"><a class="nav-link" href="{{ url_for('scrape_form') }}">新規スクレイピング</a></li>
                            <li class="nav-item"><a class="nav-link" href="{{ url_for('list_templates') }}">説明文テンプレート</a></li>
                            <li class="nav-item"><a class="nav-link" href="{{ url_for('list_shops') }}">ショップ管理</a></li>
                            <li class="nav-item"><a class="nav-link active" href="{{ url_for('support') }}">サポート</a></li>
                        </ul>
                    </div>
                </div>
            </nav>
            <h1>サポート</h1>
            <p>システムに関するお問い合わせはこちらからお願いします。</p>
            <div class="card">
                <div class="card-body">
                    <form method="POST">
                        <div class="mb-3">
                            <label for="subject" class="form-label">件名</label>
                            <input type="text" name="subject" id="subject" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label for="from_email" class="form-label">返信先メールアドレス（任意）</label>
                            <input type="email" name="from_email" id="from_email" class="form-control">
                        </div>
                        <div class="mb-3">
                            <label for="message" class="form-label">お問い合わせ内容</label>
                            <textarea name="message" id="message" class="form-control" rows="8" required></textarea>
                        </div>
                        <button type="submit" class="btn btn-primary">送信</button>
                    </form>
                </div>
            </div>
        </div>
        <script src="{{ url_for('static', filename='bootstrap/js/bootstrap.bundle.min.js') }}"></script>
    </body>
    </html>
    """
    return render_template_string(html)