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
