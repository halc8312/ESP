# バグレポート — ESP システム

## 目次

1. [バグサマリー](#バグサマリー)
2. [BUG-E2E-001: selling_price 再計算漏れ（High）](#bug-e2e-001-selling_price-再計算漏れhigh)
3. [BUG-E2E-002: scrape_jobs テーブル未作成によるジョブ失敗（High）](#bug-e2e-002-scrape_jobs-テーブル未作成によるジョブ失敗high)
4. [BUG-E2E-003: DB bootstrap モックのシグネチャ不一致（Medium）](#bug-e2e-003-db-bootstrap-モックのシグネチャ不一致medium)
5. [BUG-E2E-004: 非同期テストの pytest-asyncio 未設定（Medium）](#bug-e2e-004-非同期テストの-pytest-asyncio-未設定medium)
6. [BUG-E2E-005: /health エンドポイントが存在しない（Low）](#bug-e2e-005-health-エンドポイントが存在しないlow)

---

## バグサマリー

| Bug ID | タイトル | 深刻度 | 優先度 | ステータス |
|--------|---------|--------|--------|-----------|
| BUG-E2E-001 | selling_price が原価変更時に再計算されない | **High** | P1 | 未修正・リリースブロッカー候補 |
| BUG-E2E-002 | scrape_jobs テーブル未作成によるジョブ実行失敗 | **High** | P1 | 未修正・本番影響確認要 |
| BUG-E2E-003 | DB bootstrap モックが pool_pre_ping を受け付けない | Medium | P2 | 未修正・テスト品質の問題 |
| BUG-E2E-004 | async テストが pytest-asyncio 未設定で失敗 | Medium | P2 | 未修正・CI 品質の問題 |
| BUG-E2E-005 | /health エンドポイントが存在しない（/healthz が正） | Low | P3 | 仕様確認要 |

---

## BUG-E2E-001: selling_price 再計算漏れ（High）

| 項目 | 内容 |
|------|------|
| **Bug ID** | BUG-E2E-001 |
| **タイトル** | スクレイピング価格更新時に selling_price が再計算されない |
| **深刻度** | High |
| **優先度** | P1（リリースブロッカー候補） |
| **報告日時** | 2026-04-12 |

### 事象

`save_scraped_items_to_db()` にて商品価格（last_price）が更新された際、PricingRule が設定されている商品の `selling_price` が再計算されず、古い値のままになる。

### 前提条件

- PricingRule（margin_rate=50, shipping_cost=0, fixed_fee=0）が設定済みの商品
- 商品の last_price=1000、selling_price=1500 で初期設定済み

### 再現手順

```python
# 1. PricingRule(margin_rate=50) の商品（last_price=1000, selling_price=1500）を用意
# 2. スクレイピング更新で last_price=2000 を送信
items = [{'url': 'https://jp.mercari.com/item/m-priced?foo=bar', 'price': 2000, ...}]
save_scraped_items_to_db(items, user_id=user.id, site='mercari')
# 3. DB から selling_price を確認
```

### 期待結果

```
selling_price = 2000 * (1 + 50/100) = 3000
```

### 実際結果

```
selling_price = 1500 （更新されない）
```

### 再現率

10/10（100%）

### 影響範囲

- 全商品の価格計算（PricingRule 設定商品すべて）
- パブリックカタログに表示される `price` フィールド（`item.custom_price or p.selling_price`）
- Shopify エクスポート CSV の price フィールド
- 金銭的影響: 本番環境では誤った販売価格が表示・エクスポートされ続ける

### 暫定回避策

手動で商品詳細画面から selling_price を個別更新する（`/products/<id>` のインライン更新）

### 根本原因

`services/product_service.py` の `save_scraped_items_to_db()` 内で、価格変更後に以下を呼び出す：

```python
# services/product_service.py: ~169行目
session_db.commit()  # ← まずコミット

for product_id in repricing_product_ids:
    update_product_selling_price(product_id, session=session_db)
    # ↑ session を渡しているため owns_session=False → commit() が呼ばれない！
```

`update_product_selling_price` の実装（`services/pricing_service.py`）：

```python
def update_product_selling_price(product_id: int, session=None) -> bool:
    owns_session = session is None  # session が渡されると False
    ...
    if old_price != new_price:
        product.selling_price = new_price
        if owns_session:  # ← False のため commit() されない
            session.commit()
```

呼び出し後に `finally: session_db.close()` が実行されるため、未コミットの変更が破棄される。

### 修正方法（案）

**案1**: ループ後に明示的に commit を呼ぶ

```python
for product_id in repricing_product_ids:
    update_product_selling_price(product_id, session=session_db)
session_db.commit()  # ← 追加
```

**案2**: `update_product_selling_price` を `owns_session` に依存せず、呼び出し元が明示的に commit 責任を持つよう API を変更する

### 追加確認が必要なログ / データ / API

- 本番環境の selling_price と last_price の乖離レコード数（`SELECT COUNT(*) FROM products WHERE selling_price != (last_price * 1.5) AND pricing_rule_id IS NOT NULL`）
- スクレイピング実行ログに `selling_price updated` が記録されているか

---

## BUG-E2E-002: scrape_jobs テーブル未作成によるジョブ失敗（High）

| 項目 | 内容 |
|------|------|
| **Bug ID** | BUG-E2E-002 |
| **タイトル** | テスト環境で scrape_jobs テーブルが存在せずジョブ実行が失敗する |
| **深刻度** | High |
| **優先度** | P1（本番影響確認要） |
| **報告日時** | 2026-04-12 |

### 事象

`run_single_web_smoke()` でスクレイピングジョブを実行すると `sqlite3.OperationalError: no such table: scrape_jobs` が発生してジョブが失敗する。

### 前提条件

- テストモード（`runtime_role="test"`）
- `Base.metadata.create_all()` で DB スキーマを作成

### 再現手順

```bash
python -m pytest tests/test_cli_single_web_smoke.py -v
```

### エラーログ

```
ERROR scrape_queue:scrape_queue.py:298 Failed job dfec55a8-...: 
  (sqlite3.OperationalError) no such table: scrape_jobs
  [SQL: SELECT scrape_jobs.job_id AS scrape_jobs_job_id, ...
  FROM scrape_jobs WHERE scrape_jobs.job_id = ?]
```

### 期待結果

`snapshot["ready"] is True`

### 実際結果

`snapshot["ready"] is False`（ジョブがエラーで終了）

### 再現率

3/3（100%）

### 影響範囲

- スクレイピングジョブ追跡機能全般
- ジョブの開始・完了・エラーの状態管理
- 本番環境でも Alembic マイグレーションが未適用の場合は同様のエラーが発生する可能性

### 暫定回避策

Alembic マイグレーションを手動実行して `scrape_jobs` テーブルを作成する

### 根本原因（仮説）

`ScrapeJob` モデルが `Base.metadata` に登録されていない、または登録されているが conftest の `Base.metadata.create_all()` が呼ばれるタイミングで `ScrapeJob` が未インポートの可能性。

あるいは `ScrapeJob` モデルが Alembic 管理のみで `models.py` の `Base` に登録されていない。

### 追加確認が必要なログ / データ / API

- `models.py` に `ScrapeJob` が定義されているか
- `from models import ScrapeJob` が conftest.py より前に実行されているか
- `alembic/versions/` 内のマイグレーションファイルを確認

---

## BUG-E2E-003: DB bootstrap モックのシグネチャ不一致（Medium）

| 項目 | 内容 |
|------|------|
| **Bug ID** | BUG-E2E-003 |
| **タイトル** | DB bootstrap テストのモックが pool_pre_ping キーワード引数を受け付けない |
| **深刻度** | Medium |
| **優先度** | P2 |
| **報告日時** | 2026-04-12 |

### 事象

`test_create_app_engine_normalizes_legacy_postgres_scheme` テストで `fake_create_engine()` が `pool_pre_ping` を受け付けず TypeError が発生。

### 再現手順

```bash
python -m pytest tests/test_database_bootstrap.py::test_create_app_engine_normalizes_legacy_postgres_scheme -v
```

### エラー

```
TypeError: test_create_app_engine_normalizes_legacy_postgres_scheme.<locals>.fake_create_engine() 
  got an unexpected keyword argument 'pool_pre_ping'
```

### 根本原因

`database.py` の `create_app_engine()` に `pool_pre_ping=True` 引数が追加されたが、テストのモック関数がそのパラメータを許容していない。テストコードがプロダクションコードの変更に追従していない。

### 修正方法（案）

```python
# tests/test_database_bootstrap.py のモック関数を修正
def fake_create_engine(url, echo=False, **kwargs):  # **kwargs を追加
    ...
```

### 影響範囲

テストの信頼性のみ。本番動作には影響なし。

---

## BUG-E2E-004: 非同期テストの pytest-asyncio 未設定（Medium）

| 項目 | 内容 |
|------|------|
| **Bug ID** | BUG-E2E-004 |
| **タイトル** | async def テスト関数が pytest-asyncio プラグインなしで失敗 |
| **深刻度** | Medium |
| **優先度** | P2 |
| **報告日時** | 2026-04-12 |

### 事象

`test_scraping_client_async.py` および `test_scraping_logic.py` の async def テスト関数が実行できずに失敗する。

### 失敗テスト一覧

- `test_gather_with_concurrency_preserves_input_order_with_out_of_order_completion`
- `test_gather_with_concurrency_returns_exceptions_and_respects_cap`
- `test_collect_search_items_async_preserves_order_with_partial_failures`
- `test_run_coro_sync_is_safe_under_running_event_loop`

### エラー

```
Failed: async def functions are not natively supported.
You need to install a suitable plugin for your async framework, for example:
  - anyio
  - pytest-asyncio
```

### 修正方法（案）

```bash
pip install pytest-asyncio
# requirements.txt や pyproject.toml に追加
```

または `conftest.py` に以下を追加：

```python
# pytest.ini または pyproject.toml
[pytest]
asyncio_mode = auto
```

### 影響範囲

- 非同期スクレイピングロジックのテストカバレッジが 0%
- `gather_with_concurrency`（並行スクレイピング）のバグが未検出になるリスク
- `run_coro_sync` の イベントループ競合バグが未検出になるリスク

---

## BUG-E2E-005: /health エンドポイントが存在しない（Low）

| 項目 | 内容 |
|------|------|
| **Bug ID** | BUG-E2E-005 |
| **タイトル** | /health へのアクセスが 404 になる（正しくは /healthz） |
| **深刻度** | Low |
| **優先度** | P3 |
| **報告日時** | 2026-04-12 |

### 事象

ヘルスチェックエンドポイントが `/healthz` で実装されているが、一般的な慣例（`/health`）と異なるため、外部監視システムが誤設定になる可能性がある。

### 確認内容

```
GET /health  → 404 Not Found
GET /healthz → 200 OK, {"status":"ok", ...}
```

### 影響範囲

- Render・AWS ELB・Kubernetes の Liveness/Readiness Probe の設定ミス
- 本番エラー検知の遅れ

### 推奨対応

`/health` エンドポイントを追加するか、監視設定を `/healthz` に統一するかのドキュメント整備。
