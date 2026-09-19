from flask import Blueprint, jsonify, render_template, request
from pathlib import Path
from config import TRAINING_DIR
from web.utils import api_error_handler, start_train, get_task, start_anchor

bp = Blueprint("train", __name__)


@bp.route("/train")
def page():
    return render_template("train.html")


@bp.route("/api/train/start", methods=["POST"])
@api_error_handler
def api_train_start():
    data = request.get_json() or {}
    lottery = data.get("lottery", "双色球")
    params = {
        "iterations": data.get("iterations", 100),
        "window_list": data.get("window_list", [30, 50, 100]),
        "model_type": data.get("model_type", "statistical"),
        "patience": data.get("patience", 10),
    }
    task_id = start_train(lottery, params)
    return jsonify({"task_id": task_id})


@bp.route("/api/train/status/<task_id>")
@api_error_handler
def api_train_status(task_id):
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


@bp.route("/api/train/report/<path:record_dir>")
@api_error_handler
def api_train_report(record_dir):
    """读取训练报告的 Markdown 内容"""
    import json
    # 兼容：可能传目录名或完整路径（防御双重路径拼接）
    record_dir = Path(record_dir).name if Path(record_dir).is_absolute() else record_dir
    record_dir = record_dir.replace("\\", "/").split("/")[-1]
    report_path = Path(TRAINING_DIR) / record_dir / "report.md"
    if report_path.exists():
        content = report_path.read_text(encoding="utf-8")
        return jsonify({"content": content, "exists": True})
    return jsonify({"content": "", "exists": False})


# ------------------------------------------------------------
# 持续训练（随机锚点走前验证 + 夜间 lightgbm 重训，2026-09-18）
# ------------------------------------------------------------

@bp.route("/api/training/anchor-summary")
@api_error_handler
def api_anchor_summary():
    """锚点试验台账汇总（全量如实：不挑好的不丢差的）。"""
    from train.anchor_trainer import anchor_summary
    return jsonify(anchor_summary())


@bp.route("/api/training/anchor/start", methods=["POST"])
@api_error_handler
def api_anchor_start():
    """手动跑一轮随机锚点走前验证（异步任务，状态查 /api/train/status/<task_id>）。"""
    data = request.get_json() or {}
    lottery = data.get("lottery", "双色球")
    params = {
        "n_trials": max(1, min(int(data.get("n_trials", 10)), 200)),
        "window_size": max(10, min(int(data.get("window_size", 50)), 500)),
        "min_history": max(30, min(int(data.get("min_history", 100)), 2000)),
    }
    task_id = start_anchor(lottery, params)
    return jsonify({"task_id": task_id})


@bp.route("/api/training/loop", methods=["GET"])
@api_error_handler
def api_training_loop_status():
    from web.training_loop import get_status
    return jsonify(get_status())


@bp.route("/api/training/loop", methods=["POST"])
@api_error_handler
def api_training_loop_save():
    from web.training_loop import save_config, get_status
    data = request.get_json() or {}
    save_config(data)
    return jsonify({"ok": True, **get_status()})
