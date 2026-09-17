from flask import Blueprint, jsonify, render_template, request
from web.utils import api_error_handler, start_pipeline, get_task
from web.validators import validate_lottery

bp = Blueprint("pipeline", __name__)


@bp.route("/pipeline")
def page():
    return render_template("pipeline.html")


@bp.route("/api/pipeline/<lottery>")
@api_error_handler
@validate_lottery
def api_pipeline(lottery):
    """异步启动管道计算任务，返回 task_id"""
    task_id = start_pipeline(lottery, {"steps": [1, 2, 3, 4, 5]})
    return jsonify({"task_id": task_id})


@bp.route("/api/pipeline/status/<task_id>")
@api_error_handler
def api_pipeline_status(task_id):
    """查询管道计算任务状态"""
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
