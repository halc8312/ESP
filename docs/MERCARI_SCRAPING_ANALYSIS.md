# メルカリ・スクレイピング機能の分析レポート

## 作成日: 2025-12-18

---

## 1. 現状の実装概要

### 1.1 ファイル構成

| ファイル | 役割 |
|---------|------|
| `mercari_db.py` | メルカリのスクレイピングロジック（メイン） |
| `routes/scrape.py` | スクレイピングのWebエンドポイント |
| `services/product_service.py` | スクレイピング結果のDB保存 |
| `cli.py` | CLIからの自動更新コマンド |

> **注記:** `yahoo_db.py` はYahoo!ショッピング用のスクレイピングモジュールですが、構造はメルカリと同様のため、本分析の改善提案は両方に適用可能です。

### 1.2 主要関数

#### `mercari_db.py`
- `create_driver()` - Chrome WebDriverを生成
- `scrape_item_detail()` - 1商品ページから詳細を取得
- `scrape_shops_product()` - メルカリShops商品ページ専用
- `scrape_search_result()` - 検索結果から複数商品を取得
- `scrape_single_item()` - 単一URLからのスクレイピング

---

## 2. 現在の処理フロー（問題点の分析）

### 2.1 同期的・逐次処理の現状

```
┌─────────────────────────────────────────────────────────────────┐
│  現在の scrape_search_result() の処理フロー                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. create_driver() でWebDriver起動 (約2-5秒)                  │
│           ↓                                                     │
│  2. 検索ページにアクセス (約2-3秒)                              │
│           ↓                                                     │
│  3. スクロールして商品リンク収集 (約2秒 × max_scroll回)        │
│           ↓                                                     │
│  4. 商品ページに1件ずつアクセス (約3-5秒/件)                   │
│      ├── scrape_item_detail(url_1) → 待機                       │
│      ├── scrape_item_detail(url_2) → 待機                       │
│      ├── scrape_item_detail(url_3) → 待機                       │
│      └── ... 繰り返し (max_items回)                             │
│           ↓                                                     │
│  5. WebDriver終了                                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 時間計算の例

**10商品を検索結果から取得する場合：**

| 処理 | 時間 |
|------|------|
| WebDriver起動 | 約3秒 |
| 検索ページロード | 約3秒 |
| スクロール (3回) | 約6秒 |
| 商品詳細取得 (10件) | 約30-50秒 (3-5秒×10) |
| **合計** | **約42-62秒** |

**問題点:**
- 商品詳細の取得が完全に逐次（1件ずつ順番に処理）
- 各商品ページへのアクセスで `time.sleep()` による固定待機
- 複数リクエストの並列処理が行われていない

---

## 3. 性能改善の可能性

### 3.1 非同期化オプション

#### オプションA: asyncio + Playwright

**概要:** SeleniumからPlaywrightに移行し、asyncioで非同期処理

```python
# 改善イメージ
async def scrape_items_async(urls: list[str]):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        
        # 複数ページを並列で処理
        tasks = [scrape_single_page(browser, url) for url in urls]
        results = await asyncio.gather(*tasks)
        
        await browser.close()
        return results
```

**メリット:**
- 複数商品ページを同時にロード可能
- 10商品で約42秒 → 約10-15秒に短縮可能
- モダンなAPIで保守性向上

**デメリット:**
- Seleniumからの完全移行が必要
- Flaskとの統合に注意が必要（async対応）
- テストコードの書き換え

---

#### オプションB: concurrent.futures (ThreadPoolExecutor)

**概要:** 現在のSeleniumを維持しつつ、スレッドプールで並列化

```python
from concurrent.futures import ThreadPoolExecutor

def scrape_search_result_parallel(urls: list[str], max_workers: int = 3):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scrape_single_item, url) for url in urls]
        results = [f.result() for f in futures]
    return results
```

**メリット:**
- 既存コードの変更が最小限
- Flaskとの互換性維持
- 段階的な導入が可能

**デメリット:**
- 各スレッドで別々のWebDriverが必要（メモリ消費増）
- 同時接続数の制限が必要（サイト負荷対策）
- スレッド数に制限（3-5程度が現実的）

---

#### オプションC: Celeryによるバックグラウンド処理

**概要:** スクレイピングをバックグラウンドタスクとして非同期実行

```python
from celery import Celery

celery = Celery('tasks', broker='redis://localhost:6379')

@celery.task
def scrape_task(url):
    return scrape_single_item(url)

# 呼び出し側
def start_scraping(urls):
    job = group([scrape_task.s(url) for url in urls])
    result = job.apply_async()
    return result.id  # タスクIDを返して、後で結果を取得
```

**メリット:**
- UIがブロックされない（ユーザー体験向上）
- 大量処理に対応可能
- リトライ・エラーハンドリングが充実

**デメリット:**
- Redis/RabbitMQなどのメッセージブローカーが必要
- インフラ構成が複雑化
- 結果取得のためのポーリングUIが必要

---

### 3.2 アーキテクチャ改善オプション

#### ドライバープール

```python
class DriverPool:
    """WebDriverのプールを管理してオーバーヘッドを削減"""
    def __init__(self, size: int = 3):
        self.pool = Queue(maxsize=size)
        for _ in range(size):
            self.pool.put(create_driver())
    
    def get(self):
        return self.pool.get()
    
    def release(self, driver):
        self.pool.put(driver)
```

**効果:**
- WebDriver起動のオーバーヘッド削減
- メモリ使用量の制御

---

#### リクエストベース抽出（一部処理）

```python
import requests

def fetch_basic_info_without_selenium(url: str):
    """軽量なHTTPリクエストで基本情報のみ取得"""
    response = requests.get(url, headers={'User-Agent': '...'})
    # BeautifulSoupでパース
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # 静的HTMLから取得可能な情報のみ
    title = soup.find('h1').text
    # ...
```

**制限:**
- メルカリはJavaScriptで動的レンダリングするため、多くの情報が取得不可
- 価格、ステータス、画像URLなどはSeleniumが必要
- **結論: メルカリでは適用困難**

---

## 4. 推奨アプローチ

### 4.1 短期（すぐに実装可能）

| 改善 | 効果 | 工数 |
|------|------|------|
| `time.sleep()` の最適化 | 10-20%短縮 | 低 |
| 不要な待機の削除 | 5-10%短縮 | 低 |
| WebDriverの再利用 | 起動オーバーヘッド削減 | 低 |

**実装例: 待機時間の最適化**
```python
# Before
time.sleep(2)  # 固定2秒待機

# After (明示的な要素待機)
WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='product-name']"))
)
```

---

### 4.2 中期（ThreadPoolExecutor導入）

**推奨構成:**

```python
def scrape_search_result_v2(search_url, max_items=5, max_workers=3):
    """改善版: 商品詳細取得を並列化"""
    
    # 1. 検索結果からURLを収集（従来通り）
    driver = create_driver()
    urls = collect_item_urls(driver, search_url, max_items)
    driver.quit()
    
    # 2. 商品詳細を並列取得
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scrape_single_item, url) for url in urls]
        results = []
        for future in futures:
            try:
                items = future.result(timeout=30)
                # scrape_single_item はリストを返すため extend を使用
                if items:
                    results.extend(items)
            except Exception as e:
                logging.error(f"Scraping failed: {e}")
    
    return results
```

**期待される効果:**
- 10商品: 約42秒 → 約15-20秒 (約60%短縮)
- max_workers=3 でメモリ消費を抑制

---

### 4.3 長期（Celery + Redis）

**フル非同期アーキテクチャ:**

```
┌────────────────────────────────────────────────────────────────┐
│                   スクレイピング非同期アーキテクチャ            │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  [Flask Web UI]                                                │
│       │                                                        │
│       ├── POST /scrape/run                                     │
│       │        │                                               │
│       │        ▼                                               │
│       │   [Celery Task Queue]                                  │
│       │        │                                               │
│       │        ├── Task 1: scrape_item(url_1)                  │
│       │        ├── Task 2: scrape_item(url_2)                  │
│       │        └── Task 3: scrape_item(url_3)                  │
│       │                   │                                    │
│       │                   ▼                                    │
│       │        [Worker Pool] (並列実行)                        │
│       │                   │                                    │
│       │                   ▼                                    │
│       │        [Results Backend (Redis)]                       │
│       │                   │                                    │
│       ▼                   ▼                                    │
│   GET /scrape/status ←─ 結果取得                              │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

**必要なコンポーネント:**
- Redis (メッセージブローカー & 結果保存)
- Celery Worker (バックグラウンド処理)
- フロントエンドのポーリングUI

---

## 5. 実装優先度マトリックス

| 改善項目 | 効果 | 工数 | 優先度 |
|---------|------|------|--------|
| time.sleep() 最適化 | 中 | 低 | ★★★★★ |
| WebDriver再利用 | 中 | 低 | ★★★★☆ |
| ThreadPoolExecutor | 高 | 中 | ★★★★☆ |
| ドライバープール | 中 | 中 | ★★★☆☆ |
| Playwright移行 | 高 | 高 | ★★☆☆☆ |
| Celery導入 | 高 | 高 | ★★☆☆☆ |

---

## 6. 注意事項とリスク

### 6.1 サイト負荷対策
- 同時接続数は3-5程度に制限推奨
- リクエスト間に最低1秒の遅延を維持
- User-Agent、IPローテーションの検討

### 6.2 メモリ管理
- 並列化するとWebDriver×worker数のメモリ消費
- 低メモリ環境（Docker、PaaS等）では worker=2-3 が限界

### 6.3 エラーハンドリング
- 並列処理では個別のエラー捕捉が重要
- リトライロジックの実装を推奨

---

## 7. まとめ

### 現状の課題
1. **完全に同期的・逐次処理** - 商品が増えるほど線形に時間増加
2. **固定待機時間** - time.sleep()による無駄な待機
3. **WebDriverの毎回起動** - オーバーヘッドが大きい

### 推奨アクション
1. **即時対応**: time.sleep()を明示的な要素待機に置換
2. **次のステップ**: ThreadPoolExecutorで商品詳細取得を並列化
3. **将来的**: Celeryによる完全非同期化（大規模利用時）

### 期待される改善
- 短期改善: 10-20% の処理時間短縮
- 中期改善: 50-60% の処理時間短縮
- 長期改善: ユーザー体験の大幅向上（UIがブロックされない）
