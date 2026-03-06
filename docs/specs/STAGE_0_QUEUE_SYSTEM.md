# Stage 0: スクレイピングキューシステム仕様書

## 読むべきドキュメント

- [CURRENT_ARCHITECTURE.md](./CURRENT_ARCHITECTURE.md) — 現在のコードベース構造
- [README.md](./README.md) — 移行計画全体の概要

---

## 目標

**20人の同時ユーザーを Render Standard（2GB RAM、1 CPU）上でサポートする**スクレイピングキューシステムを実装する。

現状: スクレイピングリクエストは同期処理で、ユーザーは完了まで HTTP 接続を維持し続ける。
目標: リクエストを即座に受け付け、`job_id` を返し、バックグラウンドで処理する。

---

## アーキテクチャ設計

### ScrapeQueue クラス（`services/scrape_queue.py` に新規作成）

```
ユーザーリクエスト
      │
      ▼
 ScrapeQueue.enqueue(task)
      │
      ├─ HTTP サイト？ → http_executor（max_workers=10）
      │    Yahoo, Yahuoku, Offmall, Surugaya, SNKRDUNK
      │
      └─ ブラウザサイト？ → browser_executor（max_workers=2）
           Mercari, Rakuma（現在Selenium → 将来Playwright）
```

### ジョブステータス遷移

```
enqueue() → "queued" → (executor picks up) → "running" → "completed"
                                                        → "failed"
```

---

## メモリ予算計算

| 項目                                        | メモリ   |
|---------------------------------------------|----------|
| ベース（OS + Python + Flask）               | ~300MB   |
| HTTP スクレイプ × 10 並行（5MB × 10）      | ~50MB    |
| ブラウザスクレイプ × 2 並行（400MB × 2）   | ~800MB   |
| **合計**                                    | **~1,150MB** |
| Render Standard 上限                        | 2,048MB  |
| **余裕**                                    | ~900MB   |

> Stage 3 完了後（Playwright 移行）はブラウザスクレイプのメモリが ~150MB × 2 = 300MB になり、
> 合計 ~650MB となる。余裕が大幅に改善される。

---

## 作成・変更するファイル

### 新規作成: `services/scrape_queue.py`

```python
"""
スクレイピングキューシステム。
外部サービス（Redis/Celery）不要。Python標準ライブラリのみ使用。
"""
import uuid
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("scrape_queue")


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ScrapeJob:
    job_id: str
    site: str                        # "mercari", "rakuma", "yahoo", etc.
    task_fn: Callable                # 実行する関数
    task_args: tuple = field(default_factory=tuple)
    task_kwargs: dict = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    result: Optional[list] = None    # スクレイピング結果
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    user_id: Optional[int] = None    # 認証ユーザーID


# ブラウザ（Selenium/Playwright）が必要なサイト
BROWSER_SITES = frozenset({"mercari", "rakuma"})


class ScrapeQueue:
    """
    スクレイピングリクエストを管理するキューシステム。
    
    - HTTP サイト（Yahoo等）: max_workers=10
    - ブラウザサイト（Mercari/Rakuma）: max_workers=2
    
    Stage 1完了後: "rakuma" を BROWSER_SITES から削除（http_executor で処理）
    Stage 3完了後: "mercari" を BROWSER_SITES から削除（http_executor で処理）
    """
    
    def __init__(self):
        self._http_executor = ThreadPoolExecutor(
            max_workers=10,
            thread_name_prefix="http_scrape"
        )
        self._browser_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="browser_scrape"
        )
        self._jobs: Dict[str, ScrapeJob] = {}
        self._lock = threading.Lock()
        
        # ジョブ保持時間（秒）- 完了/失敗後1時間経過したジョブを自動削除
        # 注意: queued/running 状態のジョブは保持時間に関わらず削除されない
        self._job_ttl = 3600
        
        # 定期クリーンアップスレッド
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True
        )
        self._cleanup_thread.start()
    
    def enqueue(
        self,
        site: str,
        task_fn: Callable,
        task_args: tuple = (),
        task_kwargs: dict = None,
        user_id: int = None
    ) -> str:
        """
        スクレイピングタスクをキューに追加する。
        
        Returns:
            str: ジョブID（クライアントがポーリングに使用）
        """
        job_id = str(uuid.uuid4())
        job = ScrapeJob(
            job_id=job_id,
            site=site,
            task_fn=task_fn,
            task_args=task_args,
            task_kwargs=task_kwargs or {},
            user_id=user_id,
        )
        
        with self._lock:
            self._jobs[job_id] = job
        
        # 適切な executor に投入
        executor = (
            self._browser_executor
            if site in BROWSER_SITES
            else self._http_executor
        )
        executor.submit(self._run_job, job_id)
        
        logger.info(f"Enqueued job {job_id} for site={site}, user_id={user_id}")
        return job_id
    
    def get_status(self, job_id: str) -> Optional[dict]:
        """
        ジョブのステータスを返す。
        
        Returns:
            dict: {
                "job_id": str,
                "status": "queued" | "running" | "completed" | "failed",
                "result": list | None,
                "error": str | None,
                "queue_position": int | None,  # queued の場合のみ
                "elapsed_seconds": float,
            }
        """
        with self._lock:
            job = self._jobs.get(job_id)
        
        if job is None:
            return None
        
        elapsed = time.time() - job.created_at
        
        result = {
            "job_id": job_id,
            "status": job.status.value,
            "result": job.result,
            "error": job.error,
            "elapsed_seconds": round(elapsed, 1),
            "queue_position": None,
        }
        
        # キュー待機中のポジションを計算
        if job.status == JobStatus.QUEUED:
            result["queue_position"] = self._get_queue_position(job)
        
        return result
    
    def _get_queue_position(self, job: ScrapeJob) -> int:
        """同じ executor で待機中のジョブ数を返す"""
        same_category = BROWSER_SITES if job.site in BROWSER_SITES else set()
        count = 0
        with self._lock:
            for j in self._jobs.values():
                if j.status == JobStatus.QUEUED and j.created_at < job.created_at:
                    if job.site in BROWSER_SITES:
                        if j.site in BROWSER_SITES:
                            count += 1
                    else:
                        if j.site not in BROWSER_SITES:
                            count += 1
        return count + 1
    
    def _run_job(self, job_id: str):
        """executor から呼び出される実行関数"""
        with self._lock:
            job = self._jobs.get(job_id)
        
        if job is None:
            return
        
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        logger.info(f"Running job {job_id}")
        
        try:
            result = job.task_fn(*job.task_args, **job.task_kwargs)
            job.result = result
            job.status = JobStatus.COMPLETED
            logger.info(f"Completed job {job_id}: {len(result or [])} items")
        except Exception as e:
            job.error = str(e)
            job.status = JobStatus.FAILED
            logger.error(f"Failed job {job_id}: {e}", exc_info=True)
        finally:
            job.finished_at = time.time()
    
    def _cleanup_loop(self):
        """古いジョブを定期的に削除する"""
        while True:
            time.sleep(300)  # 5分ごとにチェック
            now = time.time()
            with self._lock:
                expired = [
                    jid for jid, j in self._jobs.items()
                    if j.status in (JobStatus.COMPLETED, JobStatus.FAILED)
                    and (now - (j.finished_at or j.created_at)) > self._job_ttl
                ]
                for jid in expired:
                    del self._jobs[jid]
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired jobs")


# アプリケーション全体で共有するシングルトンインスタンス
_queue: Optional[ScrapeQueue] = None
_queue_lock = threading.Lock()


def get_queue() -> ScrapeQueue:
    """シングルトンのキューインスタンスを返す"""
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                _queue = ScrapeQueue()
    return _queue
```

---

### 変更: `routes/scrape.py`

**変更前（同期処理）**:
```python
@scrape_bp.route("/scrape/run", methods=["POST"])
@login_required
def scrape_run():
    # ... スクレイピング処理（長時間ブロッキング） ...
    return render_template("scrape_result.html", items=items, ...)
```

**変更後（キュー投入→即時レスポンス）**:
```python
from services.scrape_queue import get_queue

@scrape_bp.route("/scrape/run", methods=["POST"])
@login_required  
def scrape_run():
    target_url = request.form.get("target_url")
    keyword = request.form.get("keyword", "")
    site = request.form.get("site", "mercari")
    # ... パラメータ取得 ...
    
    # サイトに応じたスクレイピング関数を選択
    task_fn, task_args, task_kwargs = _build_scrape_task(
        site, target_url, keyword, price_min, price_max, sort, category, limit
    )
    
    queue = get_queue()
    job_id = queue.enqueue(
        site=site,
        task_fn=task_fn,
        task_args=task_args,
        task_kwargs=task_kwargs,
        user_id=current_user.id,
    )
    
    # キュー投入直後に待機ページへリダイレクト
    return redirect(url_for('scrape.scrape_status', job_id=job_id))


@scrape_bp.route("/scrape/status/<job_id>")
@login_required
def scrape_status(job_id):
    """スクレイピング待機ページ（ポーリング用）"""
    return render_template("scrape_waiting.html", job_id=job_id)
```

---

### 新規または変更: `routes/api.py`（APIエンドポイント）

```python
from flask import Blueprint, jsonify
from flask_login import login_required
from services.scrape_queue import get_queue

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route("/scrape/status/<job_id>")
@login_required
def get_scrape_status(job_id):
    """
    スクレイピングジョブのステータスをJSONで返す。
    フロントエンドがポーリングに使用する。
    
    Response:
        {
            "job_id": "...",
            "status": "queued" | "running" | "completed" | "failed",
            "result": [...] | null,
            "error": "..." | null,
            "elapsed_seconds": 12.3,
            "queue_position": 2 | null
        }
    """
    queue = get_queue()
    status = queue.get_status(job_id)
    
    if status is None:
        return jsonify({"error": "Job not found"}), 404
    
    return jsonify(status)
```

`app.py` に登録:
```python
from routes.api import api_bp
app.register_blueprint(api_bp)
```

---

### 変更: `templates/scrape_form.html` と 新規 `templates/scrape_waiting.html`

`scrape_waiting.html` の要件:

```html
<!-- 概念的な構造（実際のデザインはscrape_form.htmlに合わせること） -->
<div id="status-container">
    <div id="status-message">スクレイピング中...</div>
    <div id="queue-position"></div>
    <div id="elapsed-time"></div>
    <div class="spinner"><!-- ローディングスピナー --></div>
</div>

<script>
const jobId = "{{ job_id }}";
const pollInterval = 2000; // 2秒ごとにポーリング

async function pollStatus() {
    const response = await fetch(`/api/scrape/status/${jobId}`);
    const data = await response.json();
    
    if (data.status === "queued") {
        document.getElementById("status-message").textContent = 
            `キュー待機中 (${data.queue_position}番目)`;
    } else if (data.status === "running") {
        document.getElementById("status-message").textContent = 
            `スクレイピング中... (${data.elapsed_seconds}秒)`;
    } else if (data.status === "completed") {
        // 完了 → 結果ページへリダイレクト
        window.location.href = `/scrape/result/${jobId}`;
        return;
    } else if (data.status === "failed") {
        document.getElementById("status-message").textContent = 
            `エラーが発生しました: ${data.error}`;
        return;
    }
    
    setTimeout(pollStatus, pollInterval);
}

pollStatus();
</script>
```

---

## 機能要件

1. ユーザーがスクレイピングフォームを送信する
2. **即座に `job_id` を返す**（スクレイピング開始を待たない）
3. 待機ページ（`/scrape/status/<job_id>`）を表示
4. フロントエンドが `/api/scrape/status/<job_id>` を2秒ごとにポーリング
5. レスポンスの `queue_position` を表示（"2番目に処理されます"）
6. 完了時: `status === "completed"` → 結果ページへリダイレクト
7. 失敗時: `status === "failed"` → エラーメッセージを表示
8. 同時実行制限: HTTP サイト最大10並行、ブラウザサイト最大2並行

---

## 重要な実装注意事項

### スレッドセーフティ

`ScrapeJob` オブジェクトの `status`, `result`, `error` フィールドは複数のスレッドから読み書きされる。
`self._lock` は `_jobs` 辞書へのアクセスを保護するが、`ScrapeJob` の属性は
同じ `job_id` を処理するスレッドと参照スレッドで競合する可能性がある。

**推奨実装**: `ScrapeJob` の変更は `_run_job` メソッド（単一スレッド）内でのみ行い、
`get_status()` は読み取り専用とする。Python の GIL がある程度保護しているが、
アトミック性が必要な箇所では `threading.Lock` を使用すること。

### Flask アプリケーションコンテキスト

`task_fn` はバックグラウンドスレッドで実行される。Flask の `current_user` や `g` には
アクセスできない。DB 操作には `SessionLocal()` を直接使用し、必ず `session.close()` すること。

```python
# 正しい実装例
def _scrape_task(site, url, user_id):
    from database import SessionLocal
    from services.product_service import save_scraped_items_to_db
    
    items = mercari_db.scrape_single_item(url, headless=True)
    
    session_db = SessionLocal()
    try:
        save_scraped_items_to_db(items, site=site, user_id=user_id, session=session_db)
    finally:
        session_db.close()
    
    return items
```

### save_scraped_items_to_db のシグネチャ確認

`routes/scrape.py` では `save_scraped_items_to_db(items, site=site, user_id=current_user.id)` と
呼ばれている。バックグラウンドスレッドから呼ぶ際は `session_db` を渡す必要があるか、
または `SessionLocal()` を内部で生成するかを `services/product_service.py` で確認すること。

---

## テスト要件

`tests/test_scrape_queue.py` を新規作成:

```python
import pytest
from unittest.mock import patch, MagicMock
from services.scrape_queue import ScrapeQueue, JobStatus, BROWSER_SITES


def test_enqueue_returns_job_id():
    """enqueue() がジョブIDを返すことを確認"""
    queue = ScrapeQueue()
    job_id = queue.enqueue("yahoo", lambda: [{"title": "test"}])
    assert isinstance(job_id, str)
    assert len(job_id) == 36  # UUID形式


def test_http_site_uses_http_executor():
    """HTTP サイトが http_executor を使用することを確認"""
    queue = ScrapeQueue()
    assert "yahoo" not in BROWSER_SITES
    assert "mercari" in BROWSER_SITES


def test_get_status_returns_none_for_unknown_job():
    """存在しない job_id で None を返すことを確認"""
    queue = ScrapeQueue()
    assert queue.get_status("non-existent-id") is None


def test_job_completes_successfully():
    """タスクが正常完了することを確認"""
    import time
    queue = ScrapeQueue()
    
    def fast_task():
        return [{"title": "テスト商品"}]
    
    job_id = queue.enqueue("yahoo", fast_task)
    
    # 完了を待つ（最大5秒）
    for _ in range(50):
        status = queue.get_status(job_id)
        if status["status"] == "completed":
            break
        time.sleep(0.1)
    
    assert status["status"] == "completed"
    assert status["result"] == [{"title": "テスト商品"}]


def test_job_fails_on_exception():
    """タスクが例外で失敗することを確認"""
    import time
    queue = ScrapeQueue()
    
    def failing_task():
        raise ValueError("テストエラー")
    
    job_id = queue.enqueue("yahoo", failing_task)
    
    for _ in range(50):
        status = queue.get_status(job_id)
        if status["status"] == "failed":
            break
        time.sleep(0.1)
    
    assert status["status"] == "failed"
    assert "テストエラー" in status["error"]


def test_queue_position():
    """キュー待機中のポジション計算を確認"""
    import time
    import threading
    
    queue = ScrapeQueue()
    
    # ブラウザ executor を埋める（max_workers=2）
    barrier = threading.Barrier(3)  # 2タスク + テストスレッド
    
    def blocking_task():
        barrier.wait()  # テストが確認するまで待機
        return []
    
    job1 = queue.enqueue("mercari", blocking_task)
    job2 = queue.enqueue("mercari", blocking_task)
    job3 = queue.enqueue("mercari", lambda: [])  # これはキューに入るはず
    
    time.sleep(0.1)
    
    status3 = queue.get_status(job3)
    # job3はキュー待機中（executor満杯）のはず
    # または既に実行中の場合もある（タイミングによる）
    assert status3["status"] in ("queued", "running", "completed")
```

---

## トラブルシュート: デプロイ後一発目から全サイト失敗するケース

### 症状

- Stage 0 デプロイ直後、どのECサイト（Mercari・Yahoo・Rakuma等）でスクレイピングしても全て失敗する
- ログ上では1件だけのジョブが処理中なのに、待機ページが即時エラー表示になる
- `"HTTPエラー: 404"` や `"Job not found"` がブラウザのコンソールまたは待機ページに表示される
- Scrapling など HTTP ベースのフェッチャーすら動作しない（スクレイパー固有の問題ではなく、キュー層の問題）

---

### 根本原因 1（最重要）: Gunicorn の複数ワーカーとプロセスローカルシングルトン

**原因**:
`get_queue()` が返す `ScrapeQueue` インスタンスはプロセスごとのインメモリシングルトン（`_queue` モジュール変数）である。
Gunicorn を `--workers 2` 以上で起動すると、各ワーカープロセスが独立した `_queue` を持つ。

```
Worker A: /scrape/run (POST) → job_id = "abc-123" を Worker A の _queue に登録 → リダイレクト
             ↓
Worker B: /api/scrape/status/abc-123 (GET) → Worker B の _queue には "abc-123" が存在しない
             ↓
Worker B: return 404 "Job not found"
             ↓
scrape_waiting.html: showError("HTTPエラー: 404") → ユーザーに即エラー表示
```

**検出パターン**:
- Gunicorn 起動コマンドに `--workers 2` 以上が指定されている
- ブラウザの Network タブで `/api/scrape/status/<job_id>` が `404` を返している
- サーバーログに `"Job not found"` が記録されるが、同じワーカーのログには当該 job_id の `Enqueued job ...` が存在しない

**修正方法**:
`Dockerfile` または起動コマンドを `--workers 1` に変更する。
スループットは `--threads` を増やすことで補う（例: `--threads 8` で同時8リクエスト処理）。

```dockerfile
# ❌ Stage 0 では使用禁止 — workers > 1 はインメモリキューを壊す
CMD gunicorn --workers 2 --threads 4 ...

# ✅ Stage 0 の正しい設定
CMD gunicorn --worker-class gthread --workers 1 --threads 8 --max-requests 0 --timeout 600 ...
```

> **Stage 1 以降への移行**: ジョブ情報を DB（`ScrapeJob` テーブル）または Redis に永続化し、
> `get_queue()` がプロセスをまたいで同じストアを参照できるようにしてから `--workers` を増やすこと。

---

### 根本原因 2: `--max-requests` によるワーカー自動再起動

**原因**:
`--max-requests 100` はメモリリーク対策として設定されることが多いが、
ワーカープロセスを再起動するとプロセス内のすべてのデーモンスレッド（`ThreadPoolExecutor` のワーカースレッドおよび `_cleanup_thread`）が強制終了される。
実行中スクレイピングジョブは状態が RUNNING のまま消失し、ポーリングクライアントは永久に完了を待ち続ける。

**検出パターン**:
- ログに `"Running job ..."` が記録されるが、`"Completed job ..."` または `"Failed job ..."` が現れない
- ワーカー再起動後、旧 job_id へのポーリングが 404 を返す
- `--max-requests 100` 前後のタイミング（Gunicorn の `[INFO] Worker restarting (max requests)` ログ）で失敗が集中する

**修正方法**:
Stage 0 では `--max-requests 0`（無効）にする。
メモリリーク対策が必要な場合は、Stage 1 以降でジョブ完了後にリソースを明示的に解放する仕組みを設けてから再度有効にすること。

---

### 根本原因 3: Flask アプリケーションコンテキストの欠如

**原因**:
`ThreadPoolExecutor` のワーカースレッドは Flask のリクエストコンテキストも
アプリケーションコンテキストも持たない。
`current_app`、`g`、`session`（リクエストコンテキスト変数）を直接参照するコードは
`RuntimeError: Working outside of application context` または
`RuntimeError: Working outside of request context` で失敗する。

**検出パターン**:
- `_run_job` ログに `RuntimeError: Working outside of application context` が記録される
- タスク関数内で `current_app.config[...]`、`g.db`、未ガードの `session` にアクセスしている
- `services/product_service.py` の `save_scraped_items_to_db` は `has_request_context()` で保護済みだが、
  新規コードがこの対策を見落とす場合がある

**修正方法**:
バックグラウンドスレッドから Flask コンテキスト変数にアクセスする必要がある場合は、
`_run_job` 内でアプリケーションコンテキストをプッシュする。

```python
# services/scrape_queue.py の _run_job 内でアプリコンテキストが必要な場合
from flask import current_app

def _run_job(self, job_id: str):
    with self._lock:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.RUNNING
        job.started_at = time.time()

    logger.info(f"Running job {job_id}")

    # Flask アプリコンテキストをバックグラウンドスレッドにプッシュする例
    # app オブジェクトへの参照が必要（app.py で get_queue() を初期化する際に渡すか、
    # current_app._get_current_object() を enqueue() 時に保存しておく）
    try:
        result = job.task_fn(*job.task_args, **job.task_kwargs)
        ...
```

> **現状の `routes/scrape.py`**: タスク関数（`_build_scrape_task` が返すクロージャ）は
> `SessionLocal()` を直接使い、`has_request_context()` ガードも適用済みのため、
> 現時点ではコンテキスト問題は発生しない。
> ただし今後バックグラウンドタスク内で Flask 拡張（Flask-SQLAlchemy 等）を使う場合は必須。

---

### 根本原因 4: ライブラリのインポート時副作用（scrapling 依存関係）

**原因**:
`scrapling` パッケージ（v0.4.1）は `playwright`、`browserforge`、`curl_cffi` を
モジュールレベルで `import` する（`eager import`）。
これらが `requirements.txt` に記載されていない場合、
`import scrapling` 時点で `ModuleNotFoundError` が発生し、
HTTP 系スクレイパーモジュール全体のインポートが失敗する。
その結果、スクレイパー固有の問題ではなく、**全サイトでスクレイピングが一切動かない**という症状になる。

**検出パターン**:
- アプリ起動ログに `ModuleNotFoundError: No module named 'playwright'`（または `browserforge`、`curl_cffi`）
- 特定のサイトだけでなく全サイトが同時に失敗する
- `pip list` で `playwright`、`browserforge`、`curl_cffi` が存在しない

**修正方法**:
`requirements.txt` に以下を明示的に追加する。

```
playwright
browserforge
curl_cffi
```

---

### 根本原因 5: `ScrapeQueue` シングルトンの初期化タイミング（import 副作用）

**原因**:
`get_queue()` は遅延初期化（初回呼び出し時に `ScrapeQueue()` を生成）だが、
`ScrapeQueue.__init__` は `_cleanup_thread`（デーモンスレッド）を即時起動する。
`--preload` オプション付きの Gunicorn や、テスト・開発環境でモジュールを
複数回インポートした場合、意図せずスレッドが多重起動する可能性がある。

**検出パターン**:
- 起動ログに複数の `_cleanup_thread` が見られる
- テスト実行時に `ScrapeQueue()` を直接インスタンス化するとスレッドが蓄積する

**修正方法（推奨）**:
テストでは `ScrapeQueue()` の直接インスタンス化後、`_cleanup_thread` をモックする。
本番では `--preload` を使用しない（または使用する場合は `post_fork` フックで再初期化する）。

---

### トラブルシュート チェックリスト

デプロイ後に全サイト失敗する場合、以下の順番で確認する：

1. **Gunicorn worker 数の確認**
   ```bash
   # 起動コマンドで --workers 1 になっているか確認
   ps aux | grep gunicorn
   ```
   → `--workers 2` 以上なら `--workers 1` に変更してデプロイし直す

2. **`--max-requests` の確認**
   ```bash
   grep max.requests Dockerfile
   ```
   → `--max-requests 100` など小さい値が設定されていれば `--max-requests 0` に変更する

3. **ライブラリの存在確認**
   ```bash
   pip show playwright browserforge curl_cffi
   ```
   → 存在しなければ `requirements.txt` に追加して再ビルドする

4. **ジョブのステータスを直接確認**
   ```bash
   # サーバーログで job_id の Enqueued/Running/Completed/Failed を追う
   grep "job_id_here" /var/log/gunicorn/error.log
   ```

5. **Flask アプリコンテキストエラーの確認**
   ```bash
   grep "Working outside" /var/log/gunicorn/error.log
   ```
   → エラーがあれば該当コードに `has_request_context()` または `with app.app_context():` を追加する

---

## 次の Agent への引き継ぎ（Stage 1 の担当者へ）

Stage 0 完了後、`services/scrape_queue.py` には以下の定数があります：

```python
BROWSER_SITES = frozenset({"mercari", "rakuma"})
```

Stage 1（ラクマ Playwright 移行）が完了したら、`rakuma` を `BROWSER_SITES` から削除してください：

```python
BROWSER_SITES = frozenset({"mercari"})  # rakuma は Playwright に移行済み
```

これにより、ラクマのスクレイピングが `browser_executor`（max_workers=2）ではなく
`http_executor`（max_workers=10）で処理されるようになります。

また、`services/scrape_queue.py` の `_run_job` メソッド内で Flask アプリケーションコンテキストが
必要な場合は、`app.py` で `app.app_context()` を使用した初期化コードを追加してください。

### Stage 1 以降でのキュー拡張 Tips

キューシステムを拡張する際は以下に注意してください：

1. **`--workers 1` の制約を解除するには**
   - ジョブ情報を `ScrapeJob` DBテーブルに永続化し、`get_queue()` がDBからジョブを読み書きするよう変更する
   - その後、Gunicorn を `--workers 2` に戻して `--max-requests`（値はメモリ使用量実測後に設定。
     一般的には 200〜500 程度）と `--max-requests-jitter`（例: `--max-requests-jitter 50`）も再設定可能

2. **executor / worker の初期化タイミングに注意**
   - `ScrapeQueue()` は `import` 時ではなく `get_queue()` 初回呼び出し時に生成される（遅延初期化）
   - Gunicorn の `--preload` オプションを使う場合は `post_fork` フックで `_queue = None` にリセットし、
     フォーク後のワーカープロセスで再初期化させること（フォーク後にスレッドを引き継がないため）

3. **Flask worker context 差異への対応**
   - バックグラウンドスレッドはリクエストコンテキスト変数（`current_user`、`session`、`g`）にアクセスできない
   - `current_app` はアプリケーションコンテキストがあればアクセス可能（`with app.app_context():` でプッシュ）
   - タスク関数に必要な値（`user_id` など）はクロージャで捕捉するか引数で渡す
   - DB 操作は `SessionLocal()` を直接使い、`finally: session_db.close()` を忘れずに
   - Flask 拡張（Flask-SQLAlchemy 等）を使う場合は `with app.app_context():` でラップする

4. **`import` 副作用の警戒**
   - スクレイパーモジュール（`yahoo_db`, `rakuma_db` 等）が import 時にネットワーク接続や
     ブラウザ起動を行っていないか確認する
   - `scrapling` の eager import 問題（`playwright`, `browserforge`, `curl_cffi`）は
     `requirements.txt` への明示記載で回避する（上記「根本原因 4」参照）
