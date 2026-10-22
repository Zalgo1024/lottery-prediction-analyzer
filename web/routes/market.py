"""
开奖数据库（market.db）浏览入口 —— 挂在主看板（5000）下的 /market

- GET  /market                页面：内嵌数据浏览器（iframe → 127.0.0.1:5001）
- GET  /api/market/status     数据浏览器在线状态 + 查询缓存概况
- POST /api/market/start      离线时后台拉起数据浏览器

★ 边界：本模块只做「页面容器 + 启停转发」，不 import market 的任何查询逻辑。
  market.db 仍由 market/viewer.py 以只读方式（mode=ro）打开，主服务不直接连它，
  因此这里没有任何写库路径，出错也不会污染开奖数据。
★ 为什么用 iframe 而不是合并成一个应用：数据浏览器是独立进程、独立端口，
  合并会把两套模板/静态资源混在一起，且它的重建缓存在长事务里会卡住主服务。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("market", __name__)

VIEWER_PORT = 5001
VIEWER_BASE = "http://127.0.0.1:%d" % VIEWER_PORT
_ROOT = Path(__file__).resolve().parents[2]
_VIEWER_SCRIPT = _ROOT / "market" / "viewer.py"
_DB_FILE = _ROOT / "market" / "market.db"

# 本进程拉起的子进程句柄（仅用于状态展示，不做强制终止）
_child = None


def _probe(timeout: float = 1.5):
    """探测数据浏览器是否在线；返回其 /healthz 的 JSON，失败返回 None。"""
    try:
        with urllib.request.urlopen(VIEWER_BASE + "/healthz", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


@bp.route("/market")
def page():
    return render_template("market.html", viewer_base=VIEWER_BASE, viewer_port=VIEWER_PORT)


@bp.route("/api/market/status")
def api_status():
    health = _probe()
    db_ok = _DB_FILE.exists()
    db_mb = round(_DB_FILE.stat().st_size / 1048576, 1) if db_ok else None
    return jsonify(
        online=health is not None,
        viewer=VIEWER_BASE,
        port=VIEWER_PORT,
        db_exists=db_ok,
        db_mb=db_mb,
        cache=(health or {}).get("cache"),
        child_pid=(_child.pid if _child is not None and _child.poll() is None else None),
        update=_update_status(),
    )


def _update_status():
    """补库状态（来自调度器，与 23:10 定时任务共享同一份状态）。"""
    try:
        from web.scheduler import scheduler
        return scheduler.market_status()
    except Exception as e:
        return {"enabled": None, "running": False, "error": str(e)[:200]}


@bp.route("/api/market/update", methods=["POST"])
def api_update():
    """手动触发补库（抓官方源，约 3~5 分钟）。

    必须异步：同步等会在浏览器/代理那侧超时。这里立刻返回「已启动」，
    前端轮询 /api/market/status 的 update.running 判断何时结束。
    """
    # ?mode=full 走全量翻页（10~20 分钟），默认增量（十几秒）
    mode = (request.args.get("mode") or "").strip().lower() or None
    try:
        from web.scheduler import scheduler
        started = scheduler.trigger_market_update(reason="manual", mode=mode)
    except Exception as e:
        return jsonify(ok=False, error="触发失败：%s" % e), 500

    if not started:
        st = _update_status()
        return jsonify(
            ok=False,
            already_running=True,
            error="已有更新任务在运行（上次开始于 %s）" % (st.get("last_run") or "刚刚"),
            update=st,
        ), 409

    return jsonify(ok=True, started=True,
                   msg="已开始更新，约 3~5 分钟；期间可继续浏览旧数据",
                   update=_update_status())


def spawn_viewer():
    """拉起数据浏览器进程，返回 (pid, error)。不探测在线状态，调用方自己判断。

    ★ 为什么单独抽出来：`run.py` 重启主服务时用 `taskkill /PID <子进程> /T /F`
      杀**整棵进程树**，而从 Flask 里 Popen 出来的 viewer 正是它的子进程 ——
      于是「重启服务」会顺手把数据浏览器一起杀掉，/market 页面变成离线。
      所以 app 启动时要主动补拉一次（见 ensure_viewer_running）。
    """
    global _child

    if not _VIEWER_SCRIPT.exists():
        return None, "找不到 market/viewer.py"
    if not _DB_FILE.exists():
        return None, "market.db 不存在，请先运行：python -m market.build"

    log_dir = _ROOT / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        log_fh = open(log_dir / "market_viewer.log", "ab")
    except OSError:
        log_fh = subprocess.DEVNULL

    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        _child = subprocess.Popen(
            [sys.executable, str(_VIEWER_SCRIPT), "--port", str(VIEWER_PORT)],
            cwd=str(_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
    except Exception as e:
        return None, "启动失败：%s" % e
    finally:
        # 子进程已继承该句柄，父进程这边要及时关闭，避免长期占用日志文件
        if log_fh is not subprocess.DEVNULL:
            try:
                log_fh.close()
            except Exception:
                pass
    return _child.pid, None


def ensure_viewer_running(wait: float = 0.0):
    """主服务启动时调用：数据浏览器没在跑就拉起来。

    放后台线程里跑（wait 秒数只是给「要不要等就绪」用），失败只记日志，
    绝不影响主服务启动 —— 它只是个可选的只读浏览进程。
    """
    if _probe(0.8) is not None:
        return {"ok": True, "already": True}
    pid, err = spawn_viewer()
    if err:
        return {"ok": False, "error": err}
    if wait > 0:
        import time as _t
        deadline = _t.time() + wait
        while _t.time() < deadline:
            if _probe(0.8) is not None:
                return {"ok": True, "started": True, "pid": pid}
            _t.sleep(0.4)
    return {"ok": True, "started": True, "pid": pid}


@bp.route("/api/market/start", methods=["POST"])
def api_start():
    """离线时后台拉起数据浏览器。已在跑则直接返回，不重复起进程。"""
    if _probe(0.8) is not None:
        return jsonify(ok=True, already=True, viewer=VIEWER_BASE)

    pid, err = spawn_viewer()
    if err:
        code = 400 if "market.db" in err else 500
        return jsonify(ok=False, error=err), code

    # 给它最多 ~12 秒完成「读库 + 建缓存」再报结果（首次建缓存约 3~10s）
    import time

    deadline = time.time() + 12
    while time.time() < deadline:
        if _probe(0.8) is not None:
            return jsonify(ok=True, started=True, pid=_child.pid, viewer=VIEWER_BASE)
        if _child.poll() is not None:
            return jsonify(
                ok=False,
                error="进程已退出（退出码 %s），详见 logs/market_viewer.log" % _child.returncode,
            ), 500
        time.sleep(0.4)

    return jsonify(
        ok=True,
        started=True,
        slow=True,
        pid=_child.pid,
        viewer=VIEWER_BASE,
        hint="进程已拉起但尚未就绪，稍等几秒再刷新",
    )


@bp.route("/api/market/rebuild", methods=["POST"])
def api_rebuild():
    """重建查询缓存。浏览器不能直接跨端口调 5001（CORS），由主服务转发一次。"""
    try:
        with urllib.request.urlopen(VIEWER_BASE + "/api/rebuild", timeout=120) as r:
            return jsonify(json.loads(r.read().decode("utf-8")))
    except Exception as e:
        return jsonify(ok=False, error="重建失败：%s" % e), 502
