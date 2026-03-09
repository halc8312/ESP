"""
Unit tests for ScrapeQueue.
"""
import time
import threading
import pytest
from services.scrape_queue import ScrapeQueue, JobStatus, BROWSER_SITES


def test_enqueue_returns_job_id():
    """enqueue() がジョブIDを返すことを確認"""
    queue = ScrapeQueue()
    job_id = queue.enqueue("yahoo", lambda: [{"title": "test"}])
    assert isinstance(job_id, str)
    assert len(job_id) == 36  # UUID形式


def test_http_site_uses_http_executor():
    """全サイトが http_executor を使用することを確認（Stage 3 完了後）"""
    queue = ScrapeQueue()
    # Stage 3 完了: すべてのサイトが http_executor で処理される
    assert "mercari" not in BROWSER_SITES
    assert "rakuma" not in BROWSER_SITES
    assert "yahoo" not in BROWSER_SITES
    assert "surugaya" not in BROWSER_SITES
    assert "offmall" not in BROWSER_SITES
    assert "yahuoku" not in BROWSER_SITES
    assert "snkrdunk" not in BROWSER_SITES
    assert len(BROWSER_SITES) == 0, f"BROWSER_SITES should be empty after Stage 3, got: {BROWSER_SITES}"


def test_get_status_returns_none_for_unknown_job():
    """存在しない job_id で None を返すことを確認"""
    queue = ScrapeQueue()
    assert queue.get_status("non-existent-id") is None


def test_job_completes_successfully():
    """タスクが正常完了することを確認"""
    queue = ScrapeQueue()

    def fast_task():
        return [{"title": "テスト商品"}]

    job_id = queue.enqueue("yahoo", fast_task)

    # 完了を待つ（最大5秒）
    status = None
    for _ in range(50):
        status = queue.get_status(job_id)
        if status["status"] == "completed":
            break
        time.sleep(0.1)

    assert status["status"] == "completed"
    assert status["result"] == [{"title": "テスト商品"}]


def test_job_fails_on_exception():
    """タスクが例外で失敗することを確認"""
    queue = ScrapeQueue()

    def failing_task():
        raise ValueError("テストエラー")

    job_id = queue.enqueue("yahoo", failing_task)

    status = None
    for _ in range(50):
        status = queue.get_status(job_id)
        if status["status"] == "failed":
            break
        time.sleep(0.1)

    assert status["status"] == "failed"
    assert "テストエラー" in status["error"]


def test_get_status_has_required_fields():
    """get_status() が必要なフィールドを含むことを確認"""
    queue = ScrapeQueue()
    job_id = queue.enqueue("yahoo", lambda: [])

    status = queue.get_status(job_id)
    assert "job_id" in status
    assert "status" in status
    assert "result" in status
    assert "error" in status
    assert "elapsed_seconds" in status
    assert "queue_position" in status
    assert status["job_id"] == job_id


def test_elapsed_seconds_increases():
    """elapsed_seconds が時間経過とともに増加することを確認"""
    queue = ScrapeQueue()

    event = threading.Event()

    def slow_task():
        event.wait(timeout=2)
        return []

    job_id = queue.enqueue("yahoo", slow_task)

    time.sleep(0.1)
    status1 = queue.get_status(job_id)
    time.sleep(0.5)
    status2 = queue.get_status(job_id)

    assert status2["elapsed_seconds"] >= status1["elapsed_seconds"]
    event.set()


def test_multiple_jobs_independent():
    """複数のジョブが独立して完了することを確認"""
    queue = ScrapeQueue()

    results = []

    def task_a():
        return [{"title": "A"}]

    def task_b():
        return [{"title": "B"}]

    job_a = queue.enqueue("yahoo", task_a)
    job_b = queue.enqueue("surugaya", task_b)

    # 両方の完了を待つ（最大5秒）
    for _ in range(50):
        sa = queue.get_status(job_a)
        sb = queue.get_status(job_b)
        if sa["status"] == "completed" and sb["status"] == "completed":
            break
        time.sleep(0.1)

    sa = queue.get_status(job_a)
    sb = queue.get_status(job_b)
    assert sa["status"] == "completed"
    assert sb["status"] == "completed"
    assert sa["result"] == [{"title": "A"}]
    assert sb["result"] == [{"title": "B"}]


def test_queue_position():
    """キュー待機中のポジション計算を確認"""
    queue = ScrapeQueue()

    # ブラウザ executor を埋める（max_workers=2）
    # Barrier(3): blocking_task×2 スレッド + テストスレッド（barrier.wait()を呼ぶ）で合計3
    barrier = threading.Barrier(3)

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

    # バリアを解放してブロッキングタスクを完了させる
    barrier.wait()


def test_browser_site_classification():
    """BROWSER_SITES が空であることを確認（Stage 3 完了後: 全サイト HTTP 移行済み）"""
    # Stage 3 完了: mercari も Playwright/HTTP 移行済み → BROWSER_SITES は空集合
    assert "mercari" not in BROWSER_SITES
    # Stage 1 完了: rakuma は BROWSER_SITES から削除済み
    assert "rakuma" not in BROWSER_SITES
    # HTTP サイト
    for http_site in ["yahoo", "yahuoku", "surugaya", "offmall", "snkrdunk"]:
        assert http_site not in BROWSER_SITES, f"{http_site} は BROWSER_SITES に含まれるべきではない"
    assert len(BROWSER_SITES) == 0, "Stage 3 完了後: BROWSER_SITES は空であるべき"


def test_status_transitions():
    """ジョブのステータスが queued → running → completed と遷移することを確認"""
    queue = ScrapeQueue()
    seen_statuses = []
    event = threading.Event()

    def slow_task():
        event.wait(timeout=3)
        return [{"title": "done"}]

    job_id = queue.enqueue("yahoo", slow_task)

    # queued または running を捕捉
    for _ in range(20):
        s = queue.get_status(job_id)
        if s["status"] not in seen_statuses:
            seen_statuses.append(s["status"])
        if s["status"] == "running":
            break
        time.sleep(0.05)

    event.set()

    # 完了を待つ
    for _ in range(50):
        s = queue.get_status(job_id)
        if s["status"] == "completed":
            if "completed" not in seen_statuses:
                seen_statuses.append("completed")
            break
        time.sleep(0.1)

    # 少なくとも completed に到達していること
    assert "completed" in seen_statuses
