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
    result: Optional[Any] = None     # スクレイピング結果
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
