"""
路由：首页 / 数据看板 / API 端点
"""

import json
from flask import Blueprint, jsonify, render_template, request

from web.utils import api_error_handler, get_all_data_status, refresh_data, compare_training_with_actual, start_update, get_task, start_auto_pipeline, start_auto_batch, start_recover_all
from web.scheduler import scheduler
from web.validators import validate_lottery

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    return render_template("dashboard.html", title="数据看板")


@bp.route("/api/data-status")
@api_error_handler
def api_data_status():
    """获取所有彩票的数据状态"""
    return jsonify(get_all_data_status())


@bp.route("/robustness")
def robustness_page():
    """鲁棒性监控页（L4 第2期：判定卡片 + 趋势图 + 告警 + worker 心跳）"""
    from config import LOTTERY_CONFIG
    return render_template("robustness.html", all_lotteries=list(LOTTERY_CONFIG.keys()))


@bp.route("/api/robustness")
@api_error_handler
def api_robustness():
    """单彩种鲁棒性报告 + 趋势（最近 60 期记录）"""
    from config import LOTTERY_CONFIG
    from credibility.robustness import ROBUSTNESS_DIR, load_trend

    lottery = request.args.get("lottery", "双色球")
    if lottery not in LOTTERY_CONFIG:
        return jsonify({"error": f"未知彩种: {lottery}"}), 400
    report = None
    try:
        f = ROBUSTNESS_DIR / f"{lottery}.json"
        if f.exists():
            report = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        report = None
    try:
        trend = load_trend(lottery)[-60:]
    except Exception:
        trend = []
    return jsonify({"lottery": lottery, "report": report, "trend": trend})


@bp.route("/api/robustness/summary")
@api_error_handler
def api_robustness_summary():
    """全彩种最新鲁棒性判定（看板顶部彩色卡片用）+ worker 心跳"""
    from config import LOTTERY_CONFIG
    from credibility.robustness import ROBUSTNESS_DIR
    from web.worker import WORKER_STATE_FILE

    lots = []
    for lot in LOTTERY_CONFIG:
        item = {"lottery": lot, "verdict": None, "alarms": [],
                "generated_at": None, "mode": None}
        try:
            f = ROBUSTNESS_DIR / f"{lot}.json"
            if f.exists():
                d = json.loads(f.read_text(encoding="utf-8"))
                item.update({
                    "verdict": d.get("verdict"),
                    "alarms": d.get("alarms") or [],
                    "generated_at": d.get("generated_at"),
                    "mode": d.get("mode"),
                })
        except Exception:
            pass
        lots.append(item)
    worker = None
    try:
        if WORKER_STATE_FILE.exists():
            worker = json.loads(WORKER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        worker = None
    return jsonify({"lotteries": lots, "worker": worker})


@bp.route("/api/refresh", methods=["POST"])
@api_error_handler
def api_refresh():
    """刷新指定彩票数据"""
    data = request.get_json() or {}
    lottery = data.get("lottery", "双色球")
    result = refresh_data(lottery)
    return jsonify(result)


@bp.route("/api/update/<lottery>", methods=["POST"])
@api_error_handler
@validate_lottery
def api_update(lottery):
    """从 500.com 抓取最新开奖数据（异步任务）"""
    task_id = start_update(lottery)
    return jsonify({"task_id": task_id})


@bp.route("/api/update/status/<task_id>")
@api_error_handler
def api_update_status(task_id):
    """查询数据抓取更新任务状态"""
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


@bp.route("/api/auto", methods=["POST"])
@api_error_handler
def api_auto():
    """一键自动流水线：抓取 → 评估 → 更新权重 → 预测

    支持批量：
    - lottery="__all__" 或留空 → 所有彩种
    - modes=["__all__"] 或 ["fresh","rule","trained"] 或单个 mode 字符串
    """
    data = request.get_json() or {}
    lottery = data.get("lottery", "双色球")
    modes = data.get("modes", data.get("mode", "fresh"))
    params = {
        "skip_predict": data.get("skip_predict", False),
        "mode": data.get("mode", "fresh"),
        # None → 由 resolve_groups 按「出号数量配置(固定/动态)」解析
        "groups": data.get("groups"),
    }
    is_batch = (lottery == "__all__") or (isinstance(modes, (list, str)) and (modes == "__all__" or modes == ["__all__"]))
    if is_batch:
        task_id = start_auto_batch(lottery, modes, params)
        return jsonify({"task_id": task_id, "batch": True})
    task_id = start_auto_pipeline(lottery, params)
    return jsonify({"task_id": task_id, "batch": False})


@bp.route("/api/auto/status/<task_id>")
@api_error_handler
def api_auto_status(task_id):
    """查询自动流水线任务状态"""
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


@bp.route("/api/automation-status")
@api_error_handler
def api_automation_status():
    """自动化流水线历史运行状态（最近运行/失败告警）"""
    from data.automation_status import get_status
    return jsonify(get_status())


@bp.route("/api/recover", methods=["POST"])
@api_error_handler
def api_recover():
    """
    手动触发启动恢复：补齐所有滞后彩种的数据并评估 pending。
    供首页「数据滞后，一键补齐」按钮调用。
    后台异步执行，立即返回 task_id 供轮询。
    """
    data = request.get_json() or {}
    force = bool(data.get("force", False))
    task_id = start_recover_all({"force": force})
    return jsonify({"task_id": task_id})


@bp.route("/compare")
def compare_page():
    """预测 vs 实际开奖对比页（支持双色球/大乐透同轨显示）"""
    return render_template("compare.html", title="预测 vs 开奖对比")


@bp.route("/api/compare/<lottery>")
@api_error_handler
@validate_lottery
def api_compare(lottery):
    """训练/预测结果 vs 最新开奖对比"""
    result = compare_training_with_actual(lottery)
    return jsonify(result)


@bp.route("/api/automation-log")
@api_error_handler
def api_automation_log():
    """读取当（或指定日期）流水线原始日志，供「查看运行明细」使用"""
    from config import BASE_DIR
    from datetime import datetime
    date = request.args.get("date") or datetime.now().strftime("%Y%m%d")
    log_file = BASE_DIR / "logs" / "automation" / f"auto_{date}.log"
    if not log_file.exists():
        return jsonify({"ok": False, "content": f"未找到 {date} 的流水线日志（自动化尚未运行或当天无运行）"})
    content = log_file.read_text(encoding="utf-8", errors="replace")
    return jsonify({"ok": True, "content": content})


# ===== 内置自动调度器（替代 Windows 任务计划程序：实时状态，无静态数据） =====
@bp.route("/api/scheduler/status")
@api_error_handler
def api_scheduler_status():
    """返回内置调度器实时状态：启用/停用、各彩种下次/上次运行、是否正在运行"""
    return jsonify(scheduler.get_status())


@bp.route("/api/scheduler/toggle", methods=["POST"])
@api_error_handler
def api_scheduler_toggle():
    data = request.get_json() or {}
    return jsonify(scheduler.set_enabled(bool(data.get("enabled", False))))


@bp.route("/api/scheduler/run-now", methods=["POST"])
@api_error_handler
def api_scheduler_run_now():
    data = request.get_json() or {}
    lottery = data.get("lottery")
    return jsonify(scheduler.run_now(lottery))
