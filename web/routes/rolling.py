"""
滚动预测训练 Web 路由

提供滚动训练页面和异步任务 API：
- GET  /rolling                           滚动训练页面
- POST /api/rolling/start                 启动滚动训练任务
- GET  /api/rolling/status/<task_id>      查询任务状态
- GET  /api/rolling/report/<path:record_dir>  读取滚动训练报告
"""

from flask import Blueprint, jsonify, render_template, request
from pathlib import Path
from config import TRAINING_DIR
from web.utils import api_error_handler, start_rolling, get_task

bp = Blueprint("rolling", __name__)


@bp.route("/rolling")
def page():
    return render_template("rolling.html")


@bp.route("/api/rolling/start", methods=["POST"])
@api_error_handler
def api_rolling_start():
    data = request.get_json() or {}
    lottery = data.get("lottery", "双色球")
    params = {
        "window_size": data.get("window_size", 50),
        "eval_periods": data.get("eval_periods", 100),
    }
    task_id = start_rolling(lottery, params)
    return jsonify({"task_id": task_id})


@bp.route("/api/rolling/status/<task_id>")
@api_error_handler
def api_rolling_status(task_id):
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


@bp.route("/api/rolling/report/<path:record_dir>")
@api_error_handler
def api_rolling_report(record_dir):
    """读取滚动训练报告的 Markdown 内容"""
    # 兼容：可能传目录名(20260807_xxx_rolling)或完整路径(E:\707\training\...)
    record_dir = Path(record_dir).name if Path(record_dir).is_absolute() else record_dir
    # 防御：去掉可能的盘符/根路径残留
    record_dir = record_dir.replace("\\", "/").split("/")[-1]
    report_path = Path(TRAINING_DIR) / record_dir / "report.md"
    if report_path.exists():
        content = report_path.read_text(encoding="utf-8")
        return jsonify({"content": content, "exists": True})
    return jsonify({"content": "", "exists": False})
