from flask import Blueprint, jsonify, render_template, request
from web.utils import api_error_handler, start_anomaly, get_task
from web.validators import validate_lottery, validate_threshold

bp = Blueprint("anomaly", __name__)


@bp.route("/anomaly")
def page():
    return render_template("anomaly.html")


@bp.route("/api/anomaly/<lottery>")
@api_error_handler
@validate_lottery
@validate_threshold
def api_anomaly(lottery):
    """异步启动异常检测任务，返回 task_id"""
    threshold = request.args.get("threshold", "normal")
    task_id = start_anomaly(lottery, {"threshold": threshold})
    return jsonify({"task_id": task_id})


@bp.route("/api/anomaly/status/<task_id>")
@api_error_handler
def api_anomaly_status(task_id):
    """查询异常检测任务状态"""
    task = get_task(task_id)
    if task is None:
        return jsonify({"status": "not_found"})
    return jsonify({
        "status": task["status"],
        "progress": task["progress"],
        "message": task["message"],
        "result": task["result"],
        "error": task["error"],
    })
