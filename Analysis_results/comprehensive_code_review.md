# ESP プロジェクト包括的コードレビュー＆改善指示書

> **目的**: 本ドキュメントは、他のAIコーディングエージェントが本リポジトリを編集する際の **実装指示書** として機能する。  
> 各改善項目に対し、対象ファイル・行番号・修正方針・擬似コードを記載する。  
> **注意**: `llama.cpp/` サブツリーはスコープ外。

---

## 目次

1. [アーキテクチャ横断 — DRY 違反・ユーティリティ統合](#1-アーキテクチャ横断--dry-違反ユーティリティ統合)
2. [データベース層](#2-データベース層)
3. [ルーティング層 — セッション管理とN+1問題](#3-ルーティング層--セッション管理とn1問題)
4. [スクレイピング層 — 巨大モジュール分割](#4-スクレイピング層--巨大モジュール分割)
5. [サービス層](#5-サービス層)
6. [セキュリティ](#6-セキュリティ)
7. [パフォーマンス最適化](#7-パフォーマンス最適化)
8. [ロギング改善](#8-ロギング改善)
9. [依存関係管理](#9-依存関係管理)
10. [テスト基盤](#10-テスト基盤)
11. [フロントエンド](#11-フロントエンド)
12. [Docker / デプロイ](#12-docker--デプロイ)

---

## 1. アーキテクチャ横断 — DRY 違反・ユーティリティ統合

### 1.1 `_env_flag` / `_as_bool` / `parse_bool` の乱立（高優先）

**現状**:  
同一ロジックの環境変数パーサーが **13箇所** に重複定義されている。

| ファイル | 関数名 |
|---------|--------|
| `app.py:665` | `_as_bool` |
| `services/worker_runtime.py:50` | `_as_bool` |
| `services/browser_pool.py:30` | `_env_flag` |
| `services/mercari_browser_fetch.py:48` | `_env_flag` |
| `services/snkrdunk_browser_fetch.py:39` | `_env_flag` |
| `services/repair_worker.py:50` | `_env_flag` |
| `services/repair_worker.py:40` | `_env_int` |
| `services/selector_healer.py:48` | `_env_float` |
| `services/selector_healer.py:58` | `_env_int` |
| `services/alerts.py:24` | `_env_int` |
| `mercari_db.py:56` | `_env_flag` |
| `security_config.py:23` | `parse_bool` |
| `worker.py:16` | `_env_to_bool` |

**修正方針**:

1. `utils/env_helpers.py`（新規ファイル）を作成し、以下を統一定義:

```python
# utils/env_helpers.py
from __future__ import annotations
import os
from typing import Any

def parse_bool(value: Any, default: bool = False) -> bool:
    """Environment variable → bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def env_flag(name: str, default: bool = False) -> bool:
    return parse_bool(os.environ.get(name), default)

def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default

def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default
```

2. 全13箇所をインポートに置換。`security_config.parse_bool` は後方互換のため残し、内部で `env_helpers.parse_bool` に委譲。

---

### 1.2 `_normalize_image_urls` / `_split_snapshot_images` の重複

**現状**:  
画像URL文字列を `|` 区切りでリスト化するロジックが少なくとも **4箇所** に存在:

- `routes/products.py:36-39` (`_split_snapshot_images`)
- `routes/main.py:157-160` (`_split_snapshot_image_urls`)
- `routes/catalog.py:84-85` (インライン)
- `services/product_service.py:46-64` (`_normalize_image_urls`)

**修正方針**:

`services/image_service.py` に以下を追加し、各呼び出し元を置換:

```python
def split_image_url_string(pipe_separated: str | None) -> list[str]:
    """'|' 区切りの画像URL文字列 → 重複除外済みリスト"""
    if not pipe_separated:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for url in pipe_separated.split("|"):
        stripped = url.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            result.append(stripped)
    return result
```

---

## 2. データベース層

### 2.1 モジュールレベルでのエンジン/セッション即時生成（中優先）

**現状** (`database.py:55-57`):

```python
engine = create_app_engine()
_session_factory = sessionmaker(bind=engine)
SessionLocal = scoped_session(_session_factory)
```

インポート時に即座にDB接続が確立される。テスト時やCLIコマンドの `--help` 実行時にも不要な接続が発生し、環境変数未設定時に即クラッシュする。

**修正方針**:

遅延初期化パターンに移行:

```python
_engine = None
_session_factory = None
_SessionLocal = None

def get_engine():
    global _engine
    if _engine is None:
        _engine = create_app_engine()
    return _engine

def get_session_local():
    global _SessionLocal, _session_factory
    if _SessionLocal is None:
        _session_factory = sessionmaker(bind=get_engine())
        _SessionLocal = scoped_session(_session_factory)
    return _SessionLocal

# 後方互換プロパティ
class _SessionLocalProxy:
    def __call__(self):
        return get_session_local()()
    def __getattr__(self, name):
        return getattr(get_session_local(), name)

SessionLocal = _SessionLocalProxy()
```

**影響範囲**: `from database import SessionLocal` は全ルート/サービスで使用されているため、Proxyオブジェクトで後方互換を維持すること。

### 2.2 `psycopg2-binary` と `psycopg[binary]` の二重インストール（低優先）

**現状** (`requirements.txt:5-6`):

```
psycopg[binary]==3.3.3
psycopg2-binary==2.9.12
```

SQLAlchemy 2.0 は psycopg3（`psycopg`）を推奨しており、psycopg2 は不要。

**修正方針**:
- `psycopg2-binary` を削除
- `database.py` の `create_app_engine` で接続文字列を `postgresql+psycopg://` とする（既に自動的に psycopg3 が使用されるが、明示が望ましい）

### 2.3 Additive Migration の二重管理（中優先）

**現状**:  
`database.py:72-103` の `ADDITIVE_STARTUP_MIGRATIONS` タプルと `alembic/versions/` のマイグレーションが **同じカラム追加** を両方管理している。

例: `products.is_listed` は:
- `database.py:101`: `ALTER TABLE products ADD COLUMN is_listed BOOLEAN DEFAULT TRUE`
- `alembic/versions/20260610_0009_add_product_is_listed.py`: 同等のAlembicマイグレーション

**修正方針**:  
Alembicマイグレーションが本番で正常動作している今、`ADDITIVE_STARTUP_MIGRATIONS` の各エントリを段階的に削除し、ランタイムパッチセットへの依存を廃止する。

手順:
1. `ADDITIVE_STARTUP_MIGRATIONS` の各行にコメントで対応Alembic revisionを記載
2. 新規カラム追加は **Alembicのみ** で行う規約を設ける
3. 次のメジャーリリースで `ADDITIVE_STARTUP_MIGRATIONS` を空タプルにし、`apply_additive_startup_migrations` を no-op 化

---

## 3. ルーティング層 — セッション管理とN+1問題

### 3.1 `try/except/finally` セッションパターンの統一（高優先）

**現状**:  
全ルートで以下のボイラープレートが **68箇所** に繰り返されている:

```python
session_db = SessionLocal()
try:
    # ... business logic ...
except Exception:
    session_db.rollback()
    raise
finally:
    session_db.close()
```

**修正方針**:

コンテキストマネージャー or デコレーターを導入:

```python
# utils/db_context.py
from contextlib import contextmanager
from database import SessionLocal

@contextmanager
def db_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

使用例:
```python
@main_bp.route("/dashboard")
@login_required
def dashboard():
    with db_session() as session_db:
        # ... business logic (commit は自動) ...
```

**注意**: 既存コードの多くは明示的に `session.commit()` を呼んでいないため、暗黙commitの追加で副作用がないか各ルートで確認すること。読み取り専用ルートは `commit()` をスキップする variant を用意してもよい。

### 3.2 一覧ページのN+1問題（高優先 — パフォーマンスに直結）

**現状** (`routes/main.py:392`):

```python
all_products = query.options(subqueryload(Product.snapshots)).all()
```

`Product.snapshots` は eager load しているが、その後の Python ループ (`_build_dashboard_product_row`) でスナップショットをソートし最新の1件だけ使用している。全スナップショットをメモリにロードしており、商品数 × スナップショット数のメモリ消費が発生。

さらに `routes/main.py:346-349`:

```python
for site in sites:
    count = base_query.filter(Product.site == site).count()
    site_stats[site] = count
```

サイト数分の個別クエリが走る（N+1パターン）。

**修正方針**:

1. `site_stats` のクエリを `GROUP BY` に統合:

```python
site_stats_rows = (
    base_query
    .with_entities(Product.site, func.count(Product.id))
    .group_by(Product.site)
    .all()
)
site_stats = dict(site_stats_rows)
```

2. スナップショットのロードを最新1件に限定:

```python
from sqlalchemy.orm import contains_eager
from sqlalchemy import select, func as sa_func

# Subquery: 各productの最新snapshot ID
latest_snap_sq = (
    select(
        ProductSnapshot.product_id,
        sa_func.max(ProductSnapshot.id).label("max_id")
    )
    .group_by(ProductSnapshot.product_id)
    .subquery()
)
```

または、`Product` モデルに `latest_snapshot_id` カラムを追加し、スナップショット保存時に更新する非正規化アプローチを検討。

### 3.3 `index()` ルートのインメモリページネーション（中優先）

**現状** (`routes/main.py:392-415`):

```python
all_products = query.options(subqueryload(Product.snapshots)).all()
# ... Python でフィルタリング ...
products_to_display = [...]  # 全件メモリ展開
paginated_products = products_to_display[offset : offset + PAGE_SIZE]
```

全商品をDBから取得し、Python側でページネーションしている。商品数が増えるとメモリとレスポンスタイムに影響。

**修正方針**:

`change_filter` の有無で分岐:
- `change_filter` なし → DB側 `LIMIT/OFFSET`
- `change_filter='changed'` → サブクエリで変更検知し、DB側フィルタリング

```python
if selected_change_filter != 'changed':
    total_items = query.count()
    paginated_products = query.offset(offset).limit(PAGE_SIZE).all()
```

---

## 4. スクレイピング層 — 巨大モジュール分割

### 4.1 `mercari_db.py` (1234行) と `surugaya_db.py` (1249行) の分割

**現状**:  
単一ファイルに検索・詳細スクレイプ・結果マージ・ブラウザフォールバックなど全ロジックが混在。

**修正方針**:

`scrapers/` ディレクトリを新設し、各サイトをパッケージ化:

```
scrapers/
├── __init__.py
├── mercari/
│   ├── __init__.py
│   ├── search.py        # scrape_search_result 関連
│   ├── detail.py        # scrape_single_item, DOM/payload マージ
│   ├── utils.py         # _env_flag, _is_nonempty_text 等
│   └── browser_fetch.py # ブラウザプール経由のフェッチ
├── surugaya/
│   ├── __init__.py
│   ├── search.py
│   ├── detail.py
│   └── parsers.py
└── ...
```

既存の `mercari_db.py` / `surugaya_db.py` は後方互換のために残し、内部で新モジュールに委譲:

```python
# mercari_db.py (互換シム)
from scrapers.mercari import scrape_search_result, scrape_single_item
```

### 4.2 `cli.py` (3624行) の分割（中優先）

**現状**: 全CLIコマンドが単一ファイルに存在。

**修正方針**:

```
cli/
├── __init__.py       # register_cli_commands (各モジュールのコマンドを集約)
├── render.py         # Render関連コマンド群
├── schema.py         # DB/マイグレーション関連
├── products.py       # 商品操作コマンド
├── diagnostics.py    # ヘルスチェック・監査系
└── exports.py        # エクスポート系
```

---

## 5. サービス層

### 5.1 `selector_healer.py` (1016行) — コードの密結合

**現状**: セレクター管理・フィンガープリント照合・修復候補生成・JSONファイルI/O が全て1ファイル。

**修正方針**:

```
services/selector_healer/
├── __init__.py       # get_healer() を re-export
├── fingerprints.py   # フィンガープリントロード/マッチング
├── healing.py        # extract_with_healing ロジック
├── persistence.py    # JSON/JSONL ファイル I/O
└── scoring.py        # 候補スコアリング
```

### 5.2 `MonitorService` のクラスメソッド設計（低優先）

**現状** (`services/monitor_service.py`):

`MonitorService` はクラスレベル辞書 (`_patrols`, `_mercari_soft_sold_counts`) を使いつつ全メソッドが `@staticmethod` で、事実上のシングルトンモジュール。

**修正方針**:

クラスをやめてモジュールレベル関数 + モジュールレベル変数に変更するか、逆に適切なインスタンス化パターン（DI可能な形）に統一する:

```python
class MonitorService:
    def __init__(self, patrols=None):
        self._patrols = patrols or _default_patrols()
        self._mercari_soft_sold_counts: dict[int, int] = {}

    def check_stale_products(self, limit=15):
        ...
```

これによりテスト時にモックパトロールを注入可能になる。

### 5.3 `pricing_service.py` — f-string ロガー（低優先）

**現状** (`services/pricing_service.py:94, 126, 132`):

```python
logger.warning(f"Product {product_id} not found")
logger.info(f"Product {product_id}: selling_price updated {old_price} -> {new_price}")
```

**修正方針**:

ログレベルのフィルタリング時に不要な文字列生成を避けるため、`%s` フォーマットに統一:

```python
logger.warning("Product %s not found", product_id)
logger.info("Product %s: selling_price updated %s -> %s", product_id, old_price, new_price)
```

プロジェクト全体で **266箇所** に f-string ロガーが存在。一括置換スクリプトで対応可能。

---

## 6. セキュリティ

### 6.1 `load_user` のエラーハンドリング不備（高優先）

**現状** (`app.py:267-276`):

```python
@login_manager.user_loader
def load_user(user_id):
    session_db = SessionLocal()
    try:
        return session_db.query(User).get(int(user_id))
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
```

`except Exception` で **全例外** を catch して rollback/raise しているが、`int(user_id)` の `ValueError` もキャッチしてしまい、不正なセッションクッキーでサーバーエラーが発生する可能性がある。

**修正方針**:

```python
@login_manager.user_loader
def load_user(user_id):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None

    session_db = SessionLocal()
    try:
        return session_db.query(User).get(uid)
    except Exception:
        session_db.rollback()
        return None
    finally:
        session_db.close()
```

### 6.2 `serve_image` のパストラバーサル保護不足（中優先）

**現状** (`app.py:371-373`):

```python
@app.route("/media/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGE_STORAGE_PATH, filename)
```

`send_from_directory` は Flask 内蔵のパストラバーサル保護を持つが、`<path:filename>` が `../` を含む場合の明示的バリデーションがない。Flask 2.3+ では安全だが、防御的コーディングとして追加チェックを推奨。

**修正方針**:

```python
from werkzeug.utils import safe_join

@app.route("/media/<path:filename>")
def serve_image(filename):
    safe_path = safe_join(IMAGE_STORAGE_PATH, filename)
    if safe_path is None:
        abort(404)
    return send_from_directory(IMAGE_STORAGE_PATH, filename)
```

### 6.3 CSRF トークン除外の文書化（低優先）

**現状** (`app.py:321-322`):

```python
from routes.bg_removal import internal_upload_bg_result
csrf.exempt(internal_upload_bg_result)
```

HMAC認証で保護されているが、内部APIのセキュリティモデルが文書化されていない。

**修正方針**: 該当コードにセキュリティコンテキストコメントを追加（HMAC検証がどこで行われているか参照リンク付き）。

---

## 7. パフォーマンス最適化

### 7.1 カタログの `_latest_snapshot` がインメモリソート（高優先）

**現状** (`routes/catalog.py:26-28`):

```python
def _latest_snapshot(product):
    if not product.snapshots:
        return None
    return sorted(product.snapshots, key=lambda s: s.scraped_at, reverse=True)[0]
```

カタログページは公開エンドポイントであり、多数の商品を一度に表示する。全スナップショットをソートして先頭1件だけ取得するのは非効率。

**修正方針**:

DBクエリ側で最新スナップショットのみを `joinedload` + `order_by` でロード:

```python
from sqlalchemy.orm import joinedload
from sqlalchemy import desc

items = (
    session_db.query(PriceListItem)
    .filter(...)
    .options(
        joinedload(PriceListItem.product)
        .joinedload(Product.snapshots.of_type(
            # latest のみ取得する subquery
        ))
    )
    .all()
)
```

または、`Product` に `latest_snapshot_image_urls` カラムを非正規化して追加し、スナップショットJOINを不要にする（推奨）。

### 7.2 `product_detail.html` テンプレートの巨大化 (3113行)

**現状**: 単一テンプレートに全編集UI・翻訳パネル・画像管理・バリアント管理が混在。

**修正方針**:

Jinja2 の `{% include %}` でセクション分割:

```
templates/
├── product_detail.html              # メインレイアウト
├── _product_detail_images.html      # 画像セクション
├── _product_detail_variants.html    # バリアントセクション  
├── _product_detail_translation.html # 翻訳パネル
└── _product_detail_pricing.html     # 価格セクション
```

### 7.3 Redis接続の多重生成（中優先）

**現状** (`app.py`):  
`_get_scheduler_heartbeat_connection` が呼ばれるたびに `Redis.from_url()` で新規接続を生成し、使用後に `close()` している。ハートビート書き込み/読み取りが頻繁に行われる場合、接続プールを活用すべき。

**修正方針**:

```python
_heartbeat_redis_pool = None

def _get_heartbeat_connection_pool(app: Flask):
    global _heartbeat_redis_pool
    if _heartbeat_redis_pool is None:
        redis_url = str(app.config.get("REDIS_URL", "") or "").strip()
        if redis_url:
            from redis import ConnectionPool
            _heartbeat_redis_pool = ConnectionPool.from_url(redis_url)
    return _heartbeat_redis_pool
```

---

## 8. ロギング改善

### 8.1 `logging.basicConfig` の複数呼び出し（中優先）

**現状**:  
`services/monitor_service.py:26` でモジュールインポート時に `logging.basicConfig(level=logging.INFO)` が呼ばれている。Flask アプリケーションのログ設定を上書きし、他モジュールのログレベルにも影響する。

**修正方針**:

`logging.basicConfig` はアプリケーションのエントリーポイント（`worker.py`, `wsgi.py`）でのみ呼び出し、各モジュールでは `logger = logging.getLogger(__name__)` のみ記述する規約にする。

`services/monitor_service.py:26` の `logging.basicConfig(level=logging.INFO)` を削除。

### 8.2 `database.py:33` の `print` ステートメント

**現状**:

```python
print(f"DEBUG: Using database URL: {debug_url}")
```

**修正方針**: `logger.debug(...)` に置換。

---

## 9. 依存関係管理

### 9.1 `pytest` が本番 requirements に含まれている（中優先）

**現状** (`requirements.txt:13-15`):

```
pytest==9.0.3
pytest-asyncio==1.3.0
pytest-flask==1.3.0
```

テスト依存が本番イメージにインストールされ、Docker イメージサイズが増加。

**修正方針**:

`requirements.txt` からテスト依存を除外し、`requirements-dev.txt` にのみ記載（既に `requirements-dev.txt` が存在するので、本番 requirements から削除するだけ）。

### 9.2 `pandas` の必要性検証（低優先）

**現状** (`requirements.txt:10`): `pandas==3.0.1` がインストールされているが、CSVエクスポートでしか使われていない可能性が高い。pandas は依存が重く（NumPy含む）イメージサイズに影響。

**修正方針**:

実際の使用箇所を確認し、`csv` 標準ライブラリ + 軽量な処理で代替可能なら pandas を除外。代替できない場合はそのまま。

---

## 10. テスト基盤

### 10.1 ルートレベルのテストファイル散乱（低優先）

**現状**:  
プロジェクトルートに `test_mercari_full.py`, `test_active_item_live.py`, `test_scraping_real.py` 等の **9個** のテストファイルが散在。`tests/` ディレクトリと統一されていない。

**修正方針**:

- `tests/integration/` ディレクトリを作成し、外部接続を伴うテストを移動
- `pytest.ini` のテストパスを `tests/` に限定し、ルートのファイルは明示的に除外するか移動

### 10.2 テストにおけるDBセッション分離

**現状** (`tests/conftest.py` を確認): テスト間でDB状態が共有される可能性がある。

**修正方針**: 各テスト関数ごとにトランザクションをロールバックする fixture を標準化。

---

## 11. フロントエンド

### 11.1 `index.html` (889行) と `product_detail.html` (3113行) のインラインJS

**現状**: テンプレート内に大量のインラインJavaScriptが埋め込まれている。

**修正方針**:

段階的に `static/js/` へ外部ファイル化:
- `static/js/product_detail_editor.js` — 商品編集フォームロジック
- `static/js/index_filters.js` — 一覧フィルタ/ソート/ページネーション

### 11.2 CSPヘッダー未設定

**現状** (`app.py:424-432`):  
`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` は設定済みだが、`Content-Security-Policy` がない。

**修正方針**:

```python
response.headers.setdefault(
    "Content-Security-Policy",
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "  # インラインJS移行後に 'unsafe-inline' を除去
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: data:; "
    "connect-src 'self'"
)
```

---

## 12. Docker / デプロイ

### 12.1 マルチステージビルド未使用（中優先）

**現状** (`Dockerfile`):  
単一ステージで pip install + ブラウザ/モデル bake + アプリコピーを実行。開発ツール・コンパイラが最終イメージに残る。

**修正方針**:

```dockerfile
# Stage 1: Builder
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim
COPY --from=builder /install /usr/local
# ... ブラウザ/モデルの bake ...
COPY . /app
```

### 12.2 ヘルスチェックの追加

**現状**: Dockerfile に `HEALTHCHECK` がない。

**修正方針**:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:${PORT:-10000}/healthz || exit 1
```

---

## 優先度マトリクス

| 優先度 | 項目 | 期待効果 |
|--------|------|----------|
| **P0 (即座)** | 3.2 N+1問題 | レスポンスタイム大幅改善 |
| **P0** | 3.3 インメモリページネーション | メモリ消費削減 |
| **P0** | 7.1 カタログ `_latest_snapshot` | 公開ページの速度改善 |
| **P1 (短期)** | 1.1 ユーティリティ統合 | 保守性向上 |
| **P1** | 3.1 セッション管理統一 | コード量削減・バグ防止 |
| **P1** | 6.1 `load_user` 修正 | セキュリティ強化 |
| **P1** | 8.1 `logging.basicConfig` 削除 | ログ品質向上 |
| **P2 (中期)** | 2.1 DB遅延初期化 | テスタビリティ向上 |
| **P2** | 2.3 Additive Migration廃止 | 技術的負債削減 |
| **P2** | 4.1 巨大モジュール分割 | 可読性・保守性 |
| **P2** | 9.1 テスト依存分離 | イメージサイズ削減 |
| **P3 (長期)** | 4.2 cli.py 分割 | 開発体験向上 |
| **P3** | 5.1 selector_healer 分割 | 保守性 |
| **P3** | 11.1 インラインJS外部化 | CSP対応・キャッシュ効率 |
| **P3** | 12.1 マルチステージビルド | イメージサイズ・セキュリティ |

---

## 実装ガイドライン

1. **各項目は独立したPRで実装**すること。大きな変更は分割PRが望ましい。
2. **後方互換性**を最優先。特に `SessionLocal`, `mercari_db`, `cli.py` の公開インターフェースを変更する場合は、互換シムを維持。
3. **テストを先に書く**: 既存テスト (`tests/test_e2e_routes.py`) が全てパスすることを各PR後に確認。
4. `llama.cpp/` は一切触れないこと。
5. `render.yaml` を変更しないこと（AGENTS.md の制約）。
6. `source_url`, `site` を公開カタログに露出させないこと（AGENTS.md のセキュリティ不変条件）。

---

*Generated: 2026-06-13 by Devin code analysis*
