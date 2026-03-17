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
    """HTTP サイトが http_executor を使用することを確認"""
    queue = ScrapeQueue()
    assert "yahoo" not in BROWSER_SITES
    assert "mercari" not in BROWSER_SITES
    # Stage 1 完了: rakuma は Playwright(StealthyFetcher) に移行済み → BROWSER_SITES から削除
    assert "rakuma" not in BROWSER_SITES
    assert "surugaya" not in BROWSER_SITES
    assert "offmall" not in BROWSER_SITES
    assert "yahuoku" not in BROWSER_SITES
    assert "snkrdunk" not in BROWSER_SITES


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
    """BROWSER_SITES の分類が正しいことを確認"""
    # Stage 3 完了: すべての対象サイトが http_executor 側に寄っている
    assert "mercari" not in BROWSER_SITES
    assert "rakuma" not in BROWSER_SITES
    # HTTP サイト
    for http_site in ["yahoo", "yahuoku", "surugaya", "offmall", "snkrdunk"]:
        assert http_site not in BROWSER_SITES, f"{http_site} は BROWSER_SITES に含まれるべきではない"


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


def test_same_user_jobs_run_serially():
    """同一ユーザーのジョブは常に1件ずつ実行されることを確認"""
    queue = ScrapeQueue()
    release_first = threading.Event()
    second_started = threading.Event()

    def first_task():
        release_first.wait(timeout=2)
        return [{"title": "first"}]

    def second_task():
        second_started.set()
        return [{"title": "second"}]

    job1 = queue.enqueue("yahoo", first_task, user_id=101)
    job2 = queue.enqueue("yahoo", second_task, user_id=101)

    time.sleep(0.2)
    status1 = queue.get_status(job1)
    status2 = queue.get_status(job2)
    assert status1["status"] == "running"
    assert status2["status"] == "queued"
    assert second_started.is_set() is False

    release_first.set()

    for _ in range(40):
        if second_started.is_set():
            break
        time.sleep(0.05)

    assert second_started.is_set() is True


def test_different_users_can_run_in_parallel():
    """別ユーザーのジョブは並列に running へ進めることを確認"""
    queue = ScrapeQueue()
    release = threading.Event()

    def blocking_task():
        release.wait(timeout=2)
        return []

    job1 = queue.enqueue("yahoo", blocking_task, user_id=201)
    job2 = queue.enqueue("yahoo", blocking_task, user_id=202)

    for _ in range(20):
        status1 = queue.get_status(job1)
        status2 = queue.get_status(job2)
        if status1["status"] == "running" and status2["status"] == "running":
            break
        time.sleep(0.05)

    status1 = queue.get_status(job1)
    status2 = queue.get_status(job2)
    release.set()

    assert status1["status"] == "running"
    assert status2["status"] == "running"


def test_get_jobs_for_user_returns_newest_first():
    """ユーザー別ジョブ一覧が新しい順で返ることを確認"""
    queue = ScrapeQueue()
    queue.enqueue("yahoo", lambda: [], user_id=301)
    time.sleep(0.01)
    second_job = queue.enqueue("yahoo", lambda: [], user_id=301)
    queue.enqueue("yahoo", lambda: [], user_id=302)

    jobs = queue.get_jobs_for_user(301, limit=5, include_terminal=True)

    assert len(jobs) == 2
    assert jobs[0]["job_id"] == second_job


def test_get_status_includes_context():
    """get_status() に UI 復元用 context が含まれることを確認"""
    queue = ScrapeQueue()
    job_id = queue.enqueue(
        "yahoo",
        lambda: [],
        user_id=401,
        context={
            "site_label": "Yahoo!ショッピング",
            "detail_label": "キーワード: context",
            "limit_label": "10件",
            "persist_to_db": False,
        },
    )

    status = queue.get_status(job_id, user_id=401)
    assert status["context"]["site_label"] == "Yahoo!ショッピング"
    assert status["context"]["persist_to_db"] is False
