# 現在のアーキテクチャ概要

> **対象読者**: 将来の AI エージェント（Playwright 移行を担当する各 Stage の実装者）
> **目的**: 移行作業の前提知識として現在のコードベース構造を正確に把握するためのリファレンスドキュメント

---

## 1. リポジトリ構成

```
ESP/
├── app.py                          # Flaskアプリのエントリポイント、APScheduler設定
├── models.py                       # SQLAlchemy ORM（Product, Variant, Shop, User）
├── database.py                     # DB接続・SessionLocal
├── requirements.txt                # Python依存ライブラリ
├── Dockerfile                      # Docker設定（Chrome + Python 3.11-slim）
│
├── mercari_db.py                   # メルカリスクレイパー（Selenium、~608行）
├── rakuma_db.py                    # ラクマスクレイパー（Selenium、~184行）
├── yahoo_db.py                     # ヤフーショッピングスクレイパー（HTTP/Scrapling）
├── yahuoku_db.py                   # ヤフオクスクレイパー（HTTP/Scrapling）
├── surugaya_db.py                  # 駿河屋スクレイパー（HTTP/Scrapling + Seleniumフォールバック）
├── offmall_db.py                   # オフモールスクレイパー（HTTP/Scrapling）
├── snkrdunk_db.py                  # SNKRDUNKスクレイパー（HTTP/Scrapling）
│
├── routes/
│   ├── scrape.py                   # スクレイピングリクエスト処理（同期）
│   ├── products.py                 # 商品一覧・詳細
│   ├── auth.py                     # 認証
│   └── ...
│
├── services/
│   ├── monitor_service.py          # パトロールサービス（15分間隔の価格監視）
│   ├── scraping_client.py          # Scrapling HTTPラッパー
│   ├── product_service.py          # 商品DB操作
│   ├── filter_service.py           # 除外フィルタ
│   └── patrol/
│       ├── base_patrol.py          # BasePatrol, PatrolResult クラス
│       ├── mercari_patrol.py       # Mercari軽量パトロール（~149行、Selenium）
│       ├── rakuma_patrol.py        # Rakuma軽量パトロール（~109行、Selenium）
│       ├── yahoo_patrol.py         # Yahoo軽量パトロール（HTTP/Scrapling）
│       ├── yahuoku_patrol.py       # ヤフオク軽量パトロール（HTTP）
│       ├── surugaya_patrol.py      # 駿河屋軽量パトロール（HTTP）
│       ├── offmall_patrol.py       # オフモール軽量パトロール（HTTP）
│       └── snkrdunk_patrol.py      # SNKRDUNK軽量パトロール（HTTP）
│
├── templates/
│   ├── scrape_form.html            # スクレイピングフォーム（同期送信・待機）
│   └── scrape_result.html          # スクレイピング結果表示
│
├── tests/
│   └── test_scraping_logic.py      # Seleniumモックを使ったユニットテスト
│
├── selector_config.py              # CSS/XPathセレクタ設定
├── scrape_metrics.py               # スクレイピング成功率メトリクス
│
└── docs/
    └── specs/                      # 本ディレクトリ（移行仕様書）
```

---

## 2. サイト別スクレイピング方式

| サイト       | ドメイン                           | 方式              | メモリ消費  | Seleniumを使用 |
|--------------|------------------------------------|-------------------|-------------|----------------|
| メルカリ     | jp.mercari.com                     | **Selenium/Chrome** | ~400MB    | ✅             |
| メルカリShops | jp.mercari.com/shops/              | **Selenium/Chrome** | ~400MB    | ✅             |
| ラクマ       | fril.jp / item.fril.jp             | **Selenium/Chrome** | ~400MB    | ✅             |
| Yahoo Shopping | shopping.yahoo.co.jp             | HTTP (Scrapling)   | ~5MB       | ❌             |
| ヤフオク     | auctions.yahoo.co.jp               | HTTP (Scrapling)   | ~5MB       | ❌             |
| 駿河屋       | suruga-ya.jp                       | HTTP (Scrapling)   | ~5MB       | ❌（フォールバック有）|
| オフモール   | netmall.hardoff.co.jp              | HTTP (Scrapling)   | ~5MB       | ❌             |
| SNKRDUNK     | snkrdunk.com                       | HTTP (Scrapling)   | ~5MB       | ❌             |

---

## 3. 現在の依存ライブラリ（requirements.txt）

```
Flask
SQLAlchemy
requests
pandas
gunicorn
selenium
webdriver-manager
Flask-Login
pytest
pytest-flask
Flask-APScheduler
undetected-chromedriver
beautifulsoup4
curl_cffi
scrapling
```

**重要**: `selenium`, `webdriver-manager`, `undetected-chromedriver` が Playwright 移行後に削除対象となる。

---

## 4. Dockerfileの現状

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# システム依存パッケージ（Chrome用ライブラリを含む）
RUN apt-get update && apt-get install -y \
    wget gnupg unzip curl \
    libxss1 fonts-liberation libasound2 libnspr4 libnss3 \
    libx11-xcb1 xdg-utils libgbm1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome インストール（公式リポジトリ経由）
RUN set -eux \
    && mkdir -p /usr/share/keyrings \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor --yes -o /usr/share/keyrings/google-linux-signing-keyring.gpg \
    && echo "deb [arch=amd64 ...] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd -m myuser
USER myuser

CMD gunicorn --worker-class gthread --workers 2 --threads 4 \
    --max-requests 100 --max-requests-jitter 20 \
    --timeout 600 --bind 0.0.0.0:${PORT:-10000} app:app
```

**Gunicorn設定まとめ**:
- `--workers 2`: 2ワーカープロセス
- `--threads 4`: 4スレッド/ワーカー（合計8同時接続処理）
- `--worker-class gthread`: スレッドベース（スクレイピング中もヘルスチェック応答可）
- `--timeout 600`: 10分タイムアウト（スクレイピングが遅い場合対応）

---

## 5. メモリ使用量の詳細

| 項目                           | メモリ消費     |
|--------------------------------|----------------|
| OS + Python + Flask ベース     | ~300MB         |
| HTTP スクレイプ（1リクエスト）  | ~5MB           |
| Selenium/Chrome（1インスタンス）| ~400MB         |
| Render Standard プラン上限      | 2GB            |

**現状の問題**: Selenium を同時に複数起動するとすぐにメモリ不足（OOM）になる。
例: 2 ユーザーが同時にメルカリ検索 → 300 + 400×2 = 1,100MB（ギリギリ）
5 ユーザー同時 → 300 + 400×5 = 2,300MB（**OOM クラッシュ**）

---

## 6. 各ファイルの主要関数

### `mercari_db.py`（~608行）

| 関数名                    | 役割                                                      |
|---------------------------|-----------------------------------------------------------|
| `_get_chrome_version()`   | インストール済みChromeのメジャーバージョン検出             |
| `create_driver(headless)` | Chrome WebDriver を生成（Render/Docker 環境最適化版）      |
| `scrape_shops_product(driver, url)` | メルカリShops商品ページのスクレイピング（バリエーション含む）|
| `scrape_item_detail(driver, url)` | メルカリ通常商品ページのスクレイピング                  |
| `scrape_search_result(search_url, max_items, max_scroll, headless)` | メルカリ検索結果から複数商品取得 |
| `scrape_single_item(url, headless)` | 1商品URLをスクレイピングしてリストで返す              |

### `rakuma_db.py`（~184行）

| 関数名                       | 役割                                                   |
|------------------------------|--------------------------------------------------------|
| `scrape_item_detail(driver, url)` | ラクマ商品ページのスクレイピング（遅延画像対応）    |
| `scrape_single_item(url, headless)` | 1商品URLをスクレイピングしてリストで返す          |
| `scrape_search_result(search_url, max_items, max_scroll, headless)` | ラクマ検索結果から複数商品取得 |

### `services/scraping_client.py`

| 関数名                   | 役割                                                      |
|--------------------------|-----------------------------------------------------------|
| `fetch_static(url)`      | Scrapling Fetcher（HTTP only）でページ取得。約5MB/リクエスト |
| `fetch_dynamic(url)`     | Scrapling StealthyFetcher（Playwright）でページ取得。将来の Selenium 代替 |
| `get_scraping_session()` | Scrapling Session（curl_cffi互換ラッパー）を返す           |

### `services/monitor_service.py`

| メソッド                          | 役割                                                          |
|-----------------------------------|---------------------------------------------------------------|
| `MonitorService.check_stale_products(limit)` | 古い商品の価格・在庫を軽量パトロールで更新（15分間隔） |
| `MonitorService._apply_patrol_result(session_db, product, result)` | パトロール結果をDBに適用 |

**重要定数**:
```python
_BROWSER_SITES = frozenset({"mercari"})  # Seleniumが必要なサイト（現在）
# ラクマも実際にはSeleniumを使用しているが、frozensetには含まれていない
# → services/patrol/rakuma_patrol.py が独自にドライバーを作成
```

### `routes/scrape.py`

| ルート             | メソッド | 役割                                                           |
|--------------------|----------|----------------------------------------------------------------|
| `/scrape`          | GET/POST | スクレイピングフォーム表示                                     |
| `/scrape/run`      | POST     | スクレイピング実行（**同期処理** - リクエスト中ブロック）      |

**現状の問題**: `/scrape/run` は同期処理のため、スクレイピング完了まで HTTP 接続を維持し続ける。
複数ユーザーが同時送信すると Gunicorn スレッド数（8）が枯渇する。

---

## 7. データベーススキーマ概要

### `products` テーブル（主要フィールド）

| フィールド          | 型       | 説明                               |
|---------------------|----------|------------------------------------|
| `id`               | Integer  | 主キー                             |
| `user_id`          | Integer  | オーナーユーザーID（FK）           |
| `site`             | String   | サイト識別子（"mercari", "rakuma" 等）|
| `source_url`       | String   | 元商品URL                          |
| `last_title`       | String   | 最終取得タイトル                   |
| `last_price`       | Integer  | 最終取得価格（円）                 |
| `last_status`      | String   | 最終取得ステータス（on_sale/sold） |
| `archived`         | Boolean  | アーカイブ済みフラグ               |
| `updated_at`       | DateTime | 最終更新日時（パトロール更新対象） |

### `variants` テーブル

| フィールド          | 型       | 説明                               |
|---------------------|----------|------------------------------------|
| `product_id`       | Integer  | 親商品ID（FK）                     |
| `option1_value`    | String   | バリエーション値1（色など）        |
| `option2_value`    | String   | バリエーション値2（サイズなど）    |
| `price`            | Integer  | バリエーション価格                 |
| `inventory_qty`    | Integer  | 在庫数                             |

---

## 8. パトロールシステム詳細

### 仕組み

1. `app.py` で APScheduler を起動（`fcntl.flock` で1プロセスのみ実行）
2. 15分ごとに `MonitorService.check_stale_products(limit=15)` を呼び出し
3. `updated_at` の古い順に最大15商品を選択
4. `_BROWSER_SITES` に含まれる場合は共有 Chrome ドライバーを使用
5. それ以外は Scrapling（HTTP only）を使用

### パトロール結果（PatrolResult）

```python
@dataclass
class PatrolResult:
    price: Optional[int] = None
    status: Optional[str] = None  # "active" or "sold"
    variants: List[dict] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
```

---

## 9. Scrapling の現状

`scrapling` ライブラリはすでに `requirements.txt` に含まれており、`services/scraping_client.py` で使用されている。

- `Fetcher.get()` → HTTP-only（curl_cffi ベース）→ **本番稼働中**
- `StealthyFetcher.fetch()` → Playwright ベース → **未稼働**（`python -m scrapling install` が必要）

Playwright ブラウザのインストールは Dockerfile に追加が必要（Stage 1 の作業）。

---

## 10. 現状の課題まとめ

1. **メモリ問題**: Selenium Chrome が1インスタンス約400MB消費。同時スクレイピング数を制限できていない
2. **同期ブロッキング**: `routes/scrape.py` が同期処理でユーザーを長時間待機させる
3. **スケーラビリティ**: 20ユーザー同時接続に対応できない
4. **重いDockerイメージ**: Chrome + ChromeDriver により ~1.5GB のイメージサイズ

これらを解決するのが Stage 0〜4 の移行計画。
