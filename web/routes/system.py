"""
系统控制路由：服务状态查询 + 重启触发。

重启原理：
  POST /api/system/restart 仅写入 config/restart_flag 标志文件，由 run.py 看门狗
  负责“杀旧子进程 -> 清标志 -> 拉新子进程”，从而保证加载磁盘上的最新代码。
  这样做比在 Flask 进程内自重启更可靠：避免了“旧进程还占着 5000 端口、新进程绑定失败”的竞态。
"""

import os
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

bp = Blueprint("system", __name__)

# config/restart_flag 路径（与 run.py 共用）
_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config"
)
RESTART_FLAG = os.path.join(_CONFIG_DIR, "restart_flag")

_START_TIME = time.time()


@bp.route("/api/system/status")
def system_status():
    """返回服务进程信息（实时，非静态）。"""
    try:
        pid = os.getpid()
        uptime = int(time.time() - _START_TIME)
        flag_pending = os.path.exists(RESTART_FLAG)
        return jsonify({
            "ok": True,
            "pid": pid,
            "uptime_seconds": uptime,
            "started_at": datetime.fromtimestamp(_START_TIME, tz=timezone.utc).isoformat(),
            "restart_pending": flag_pending,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.route("/api/system/invariants")
def system_invariants():
    """「出号数量 == 下一期 pending 组数」一致性体检（看板常驻徽章数据源）。

    2026-10-21：把这条等式做成接口，是因为五次覆盖事故的共同点都是**静默** ——
    机器在跑、日志全绿，页面上看不出滑动注数被换成了 5。判据集中在一处，
    看板与启动自愈（web/startup_recovery.ensure_pending_groups）共用同一份口径。
    """
    try:
        from web.utils import groups_invariant
        rep = groups_invariant()
        rep["ok"] = True
        return jsonify(rep)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.route("/api/system/digest")
def system_digest():
    """离线体检周报（只读）：过去 N 天的运行事实汇总。

    2026-10-21：此前 `scripts/health_check.py` 写得很完整却**没有任何调用方**，
    等于只能靠人手动叫醒 —— 这恰好把维护绑死在"人得在线"上。现在它由
    `web/health_digest.py` 在调度器 tick 里到点触发，本接口负责把同一份事实
    给页面看（与推送同源，口径不会分叉）。
    """
    try:
        days = request.args.get("days", default=7, type=int) or 7
        from web.health_digest import cached_digest
        # 浅拷贝：缓存对象不能被接口层塞字段（否则下一次请求读到的就是上一条的 ok）
        rep = dict(cached_digest(days))
        rep["ok"] = True
        return jsonify(rep)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.route("/api/system/restart", methods=["POST"])
def system_restart():
    """触发重启：写入 restart_flag，由 run.py 看门狗接管实际重启。"""
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(RESTART_FLAG, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return jsonify({
            "ok": True,
            "msg": "已请求重启，约 1~2 秒后服务将以最新代码重新就绪，请稍候刷新页面。",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500
