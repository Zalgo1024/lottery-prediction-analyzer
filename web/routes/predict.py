from flask import Blueprint, jsonify, render_template, request
from web.utils import api_error_handler, start_predict, get_task
from web.validators import validate_predict_body

bp = Blueprint("predict", __name__)


@bp.route("/predict")
def page():
    return render_template("predict.html")


@bp.route("/api/predict", methods=["POST"])
@api_error_handler
@validate_predict_body
def api_predict():
    """异步启动预测任务，返回 task_id"""
    data = request.get_json(silent=True) or {}
    lottery = data.get("lottery", "双色球")
    # groups 留空 → 由 resolve_groups 按「出号数量配置(固定/动态)」解析
    groups = data.get("groups")
    mode = data.get("mode", "fresh")

    task_id = start_predict(lottery, {"groups": groups, "mode": mode})
    return jsonify({"task_id": task_id})


@bp.route("/api/predict/status/<task_id>")
@api_error_handler
def api_predict_status(task_id):
    """查询预测任务状态"""
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
