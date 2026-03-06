"""
API routes for scraping job status polling.
"""
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
            "result": {...} | null,
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
