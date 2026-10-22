"""
Flask Web 应用入口
启动：python web/app.py [--port 5000]
"""

import argparse
import sys
import os

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 2026-10-21：stdout/stderr 一律 UTF-8。
#   本进程的输出通常被 run.py 重定向进 logs/flask_stdout.log（以 UTF-8 打开）。
#   Windows 下 Python 默认按本地代码页（cp936）写 fd 1 ⇒ 中文变乱码、emoji 直接
#   抛 UnicodeEncodeError（日志里表现为半截堆栈）。这里显式改 UTF-8 并容错降级，
#   不再依赖启动器是否传了 PYTHONIOENCODING。
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from flask import Flask, send_from_directory, request
from pathlib import Path

from config import TRAINING_DIR
from logs.logger import setup_logger
from web.scheduler import scheduler
from web.worker import start_worker, get_worker

app = Flask(__name__)
app.config["TITLE"] = "彩票预测分析系统"
app.config["LOTTERIES"] = ["双色球", "大乐透"]
# 模板改动即时生效（每次请求重新加载模板），避免“改了 HTML 浏览器仍是旧版”的困惑
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


# 挂载 training/ 目录用于图表图片
@app.route("/training/<path:filename>")
def serve_training_file(filename):
    return send_from_directory(TRAINING_DIR, filename)


# 注册路由
from web.routes.dashboard import bp as dashboard_bp
from web.routes.stats import bp as stats_bp
from web.routes.pipeline import bp as pipeline_bp
from web.routes.train import bp as train_bp
from web.routes.rolling import bp as rolling_bp
from web.routes.predict import bp as predict_bp
from web.routes.anomaly import bp as anomaly_bp
from web.routes.history import bp as history_bp
from web.routes.feedback import bp as feedback_bp
from web.routes.records import bp as records_bp
from web.routes.ev import bp as ev_bp
from web.routes.hits import bp as hits_bp
from web.routes.system import bp as system_bp
from web.routes.audit import bp as audit_bp
from web.routes.settings import bp as settings_bp
from web.routes.market import bp as market_bp

app.register_blueprint(dashboard_bp)
app.register_blueprint(stats_bp)
app.register_blueprint(pipeline_bp)
app.register_blueprint(train_bp)
app.register_blueprint(rolling_bp)
app.register_blueprint(predict_bp)
app.register_blueprint(anomaly_bp)
app.register_blueprint(history_bp)
app.register_blueprint(feedback_bp)
app.register_blueprint(records_bp)
app.register_blueprint(ev_bp)
app.register_blueprint(hits_bp)
app.register_blueprint(system_bp)
app.register_blueprint(audit_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(market_bp)


# 禁用浏览器缓存：静态资源 + HTML 页面
# - 静态资源：保证前端 JS/CSS 改动后浏览器立即生效
# - HTML 页面：**页面本身也必须禁缓存**。此前只覆盖了 /static/，于是浏览器会对
#   /hits 这类动态页做启发式缓存，「改了模板、刷新后还是旧版」的困惑就是这么来的。
@app.after_request
def _no_cache(resp):
    if request.path.startswith("/static/") or resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# 资源版本戳：按静态文件修改时间生成 ?v= 参数，JS/CSS 改动后浏览器强制重新拉取，
# 与上面的 no-cache 双保险，杜绝“改了代码按钮仍是旧行为”的浏览器缓存问题
@app.context_processor
def _inject_asset_version():
    static_dir = Path(app.static_folder)
    def asset_ver(rel):
        p = static_dir / rel
        try:
            return int(os.path.getmtime(p))
        except OSError:
            return 0
    return {"asset_ver": asset_ver}


# 导航单一真源：顶栏与侧栏都从 web/nav.py 的 NAV_GROUPS 渲染，
# 杜绝「某页只在顶栏有、侧栏没有」的漂移。
@app.context_processor
def _inject_nav():
    from web.nav import NAV_GROUPS, nav_flat
    return {"nav_groups": NAV_GROUPS, "nav_flat": nav_flat()}


def main():
    parser = argparse.ArgumentParser(prog="web", description="启动 Web 界面")
    parser.add_argument("--port", type=int, default=5000, help="端口号（默认 5000）")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    log = setup_logger("web")
    log.info(f"启动 Web 服务: http://127.0.0.1:{args.port}")
    # 启动内置自动调度器（替代 Windows 定时任务：开奖日后自动跑流水线，单进程、实时状态）
    scheduler.start()

    # 启动持续计算核心（L4 第2期形态A：队列消费 + 事件驱动鲁棒性监控 + 心跳）
    # 容错：worker 属于可选子系统，任何异常都不能拖死 Flask 主进程
    try:
        start_worker(daemon=True)
    except Exception as e:
        log.warning(f"持续计算核心启动失败（Web 服务继续运行）: {e}")

    # 启动时数据恢复：若程序关机/重启导致数据滞后，后台自动补齐（不等 22:05 调度）
    from web.startup_recovery import run_startup_recovery
    run_startup_recovery()

    # 持续训练循环：空闲时段随机锚点走前验证 + 夜间 lightgbm 重训（可选子系统）
    try:
        from web.training_loop import start_training_loop
        start_training_loop(daemon=True)
    except Exception as e:
        log.warning(f"持续训练循环启动失败（Web 服务继续运行）: {e}")

    # 后台预热数据缓存：启动时加载所有彩种到内存，首次访问首页/统计页秒开
    import threading
    from config import LOTTERY_CONFIG
    from data.loader import load_lottery

    def _preload_cache():
        for _name in LOTTERY_CONFIG:
            try:
                load_lottery(_name)
                log.info(f"预热缓存完成: {_name}")
            except Exception as e:
                log.warning(f"预热缓存失败: {_name} — {e}")

    threading.Thread(target=_preload_cache, daemon=True).start()

    # 数据浏览器（/market 内嵌页的 5001 服务）：重启主服务时 run.py 用
    # `taskkill /T` 杀整棵进程树，会把它一起带走，所以这里补拉一次。
    # 它只是可选的只读浏览进程，起不来也不该影响主服务。
    def _ensure_viewer():
        try:
            from web.routes.market import ensure_viewer_running
            r = ensure_viewer_running(wait=10)
            log.info(f"数据浏览器自启动: {r}")
        except Exception as e:
            log.warning(f"数据浏览器自启动失败（不影响主服务）: {e}")

    threading.Thread(target=_ensure_viewer, daemon=True).start()

    try:
        app.run(host="0.0.0.0", port=args.port, debug=args.debug)
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
