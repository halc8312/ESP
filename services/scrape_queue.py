"""
スクレイピングキューシステム。
外部サービス（Redis/Celery）不要。Python標準ライブラリのみ使用。

⚠️  デプロイ上の重要な制約 ⚠️
このモジュールはプロセス内インメモリシングルトン（_queue）を使用する。
Gunicorn を --workers 2 以上で起動すると、ジョブを作成したワーカープロセスと
ステータスをポーリングするワーカープロセスが異なる場合があり、
「Job not found (404)」 → 待機ページの即時エラー表示が発生する。

Stage 0 では必ず --workers 1 で起動すること。
Stage 1 以降でジョブをDBまたはRedisに永続化してから --workers を増やすこと。

また、--max-requests によるワーカー自動再起動はバックグラウンドスレッドを強制終了するため
実行中スクレイピングジョブが消失する。Stage 0 では --max-requests 0 で無効化すること。
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
    result: Optional[Any] = None     # スクレイピング結果
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    user_id: Optional[int] = None    # 認証ユーザーID
    context: Dict[str, Any] = field(default_factory=dict)
    dispatched: bool = False         # executor へ投入済みかどうか（queued の二重投入防止）


# ブラウザ（Selenium/Playwright）が必要なサイト
# Stage 1 完了: "rakuma" を削除（StealthyFetcher / Playwright で http_executor から処理）
BROWSER_SITES = frozenset()


class ScrapeQueue:
    """
    スクレイピングリクエストを管理するキューシステム。

    - HTTP サイト（Yahoo等）: max_workers=10
    - ブラウザサイト（Mercari/Rakuma）: max_workers=2

    Stage 1完了後: "rakuma" を BROWSER_SITES から削除（http_executor で処理）
    Stage 3完了後: "mercari" を BROWSER_SITES から削除（http_executor で処理）
    """

    def __init__(self):
        self._http_max_workers = 10
        self._browser_max_workers = 2
        self._http_executor = ThreadPoolExecutor(
            max_workers=self._http_max_workers,
            thread_name_prefix="http_scrape"
        )
        self._browser_executor = ThreadPoolExecutor(
            max_workers=self._browser_max_workers,
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
        user_id: int = None,
        context: Optional[dict] = None,
    ) -> str:
        """
        スクレイピングタスクをキューに追加する。

        task_fn の戻り値は dict または list を想定する。
        routes/scrape.py の _build_scrape_task が返す dict 形式の場合:
            {
                "items": list[dict],
                "new_count": int,
                "updated_count": int,
                "error_msg": str,
                "search_url": str,
                ...
            }
        単純なリストを返す場合は job.result にそのまま格納される。

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
            context=context or {},
        )

        with self._lock:
            self._jobs[job_id] = job

        logger.info(f"Enqueued job {job_id} for site={site}, user_id={user_id}")
        self._dispatch_queued_jobs()
        return job_id

    def get_status(self, job_id: str, user_id: int = None) -> Optional[dict]:
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
        if user_id is not None and job.user_id != user_id:
            return None

        return self._serialize_job(job)

    def get_jobs_for_user(
        self,
        user_id: int,
        limit: int = 5,
        include_terminal: bool = True,
    ) -> list[dict]:
        """指定ユーザーのジョブ一覧を新しい順で返す。"""
        safe_limit = max(1, int(limit or 5))
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.user_id == user_id]

        if not include_terminal:
            jobs = [job for job in jobs if job.status in (JobStatus.QUEUED, JobStatus.RUNNING)]

        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return [self._serialize_job(job) for job in jobs[:safe_limit]]

    def _serialize_job(self, job: ScrapeJob) -> dict:
        elapsed = time.time() - job.created_at
        result = {
            "job_id": job.job_id,
            "site": job.site,
            "status": job.status.value,
            "result": job.result,
            "error": job.error,
            "elapsed_seconds": round(elapsed, 1),
            "queue_position": None,
            "context": job.context or {},
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }

        if job.status == JobStatus.QUEUED:
            result["queue_position"] = self._get_queue_position(job)

        return result

    def _get_executor_key(self, site: str) -> str:
        return "browser" if site in BROWSER_SITES else "http"

    def _get_executor(self, site: str) -> ThreadPoolExecutor:
        return self._browser_executor if site in BROWSER_SITES else self._http_executor

    def _get_executor_limit(self, executor_key: str) -> int:
        return self._browser_max_workers if executor_key == "browser" else self._http_max_workers

    def _dispatch_queued_jobs(self):
        """空き worker と user ごとの実行制約に合わせて queued job を投入する。"""
        submissions: list[tuple[ThreadPoolExecutor, str]] = []

        with self._lock:
            active_counts = {"browser": 0, "http": 0}
            active_user_ids = set()

            for job in self._jobs.values():
                if job.status == JobStatus.RUNNING or job.dispatched:
                    executor_key = self._get_executor_key(job.site)
                    active_counts[executor_key] += 1
                    if job.user_id is not None:
                        active_user_ids.add(job.user_id)

            queued_jobs = sorted(
                (
                    job for job in self._jobs.values()
                    if job.status == JobStatus.QUEUED and not job.dispatched
                ),
                key=lambda job: job.created_at,
            )

            for job in queued_jobs:
                executor_key = self._get_executor_key(job.site)
                if active_counts[executor_key] >= self._get_executor_limit(executor_key):
                    continue
                if job.user_id is not None and job.user_id in active_user_ids:
                    continue

                job.dispatched = True
                active_counts[executor_key] += 1
                if job.user_id is not None:
                    active_user_ids.add(job.user_id)
                submissions.append((self._get_executor(job.site), job.job_id))

        for executor, job_id in submissions:
            try:
                executor.submit(self._run_job, job_id)
            except Exception as exc:
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is not None:
                        job.dispatched = False
                        job.status = JobStatus.FAILED
                        job.error = str(exc)
                        job.finished_at = time.time()
                logger.error(f"Failed to submit job {job_id}: {exc}", exc_info=True)

    def _get_queue_position(self, job: ScrapeJob) -> int:
        """同じ executor で待機中のジョブ数を返す"""
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
            job.dispatched = False

        logger.info(f"Running job {job_id}")

        try:
            result = job.task_fn(*job.task_args, **job.task_kwargs)
            with self._lock:
                job.result = result
                job.status = JobStatus.COMPLETED
                job.finished_at = time.time()
            items = result.get("items", []) if isinstance(result, dict) else (result or [])
            logger.info(f"Completed job {job_id}: {len(items)} items")
        except Exception as e:
            with self._lock:
                job.error = str(e)
                job.status = JobStatus.FAILED
                job.finished_at = time.time()
            logger.error(f"Failed job {job_id}: {e}", exc_info=True)
        finally:
            self._dispatch_queued_jobs()

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
