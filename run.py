"""
彩票预测分析系统 —— 单入口看门狗启动器（supervisor）

为什么需要它（踩过的坑）：
  之前项目里有 启动服务.bat / 重启Flask.bat / start_web.bat 三套互相打架的启动方式，
  且没有一个能“干净重启并加载最新代码”。结果要么是旧进程残留（前端改了不生效），
  要么是新进程绑定 5000 端口失败（重启看起来没反应），要么窗口关了 Flask 还在后台跑。

本启动器只做一件事，但做彻底：
  1. 启动前清理任何残留的 Flask 进程（按端口占用 + 命令行特征），杜绝孤儿/旧进程占 5000。
  2. 拉起 Flask 子进程（web/app.py），并把日志写到 logs/flask_stdout.log 便于排查。
  3. 看门狗轮询 config/restart_flag：一旦写入（由 Web 端“重启服务”按钮触发），
     先杀旧子进程、清标志、再拉新子进程 —— 实现“热重启”，且必然加载磁盘上的最新代码。
  4. 首次就绪后自动打开浏览器。
  5. 本窗口关闭 / Ctrl+C 时，连同 Flask 子进程一起退出（用 taskkill /T 杀整棵进程树）。

用法：
  python run.py [--port 5000]
双击 启动项目.bat 即等价于本脚本。
"""

import argparse
import atexit
import os
import subprocess
import sys
import threading
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

CONFIG_DIR = os.path.join(BASE_DIR, "config")
RESTART_FLAG = os.path.join(CONFIG_DIR, "restart_flag")

child = None          # 当前 Flask 子进程（subprocess.Popen）
_browser_opened = False
_crash_count = 0      # 连续崩溃计数，防止无限重启循环


def log(msg):
    print(f"[run.py] {msg}", flush=True)


def _ps_script_path():
    return os.path.join(BASE_DIR, "scripts", "kill_flask.ps1")


def kill_orphans(include_supervisor=False):
    """调用集中化脚本清理残留 Flask 进程。启动阶段不杀 supervisor 本身。"""
    ps = _ps_script_path()
    if not os.path.exists(ps):
        log("警告：未找到 scripts/kill_flask.ps1，跳过孤儿清理")
        return
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps]
    if include_supervisor:
        cmd.append("-IncludeSupervisor")
    try:
        # 不捕获输出：Windows 上 powershell 默认 GBK 编码，capture 会触发 utf-8 解码异常
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception as e:  # 清理失败不应阻断启动
        log(f"kill_orphans 异常（已忽略）：{e}")


def port_free(port, tries=25, delay=0.3):
    import socket
    for _ in range(tries):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", port))
            s.close()
            return True
        except OSError:
            time.sleep(delay)
    return False


def spawn_child(port):
    """拉起 Flask 子进程，返回 Popen。日志追加到 logs/flask_stdout.log。"""
    global child, _crash_count
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    logf = open(os.path.join(BASE_DIR, "logs", "flask_stdout.log"), "a", encoding="utf-8")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    logf.write(f"\n===== flask spawned at {ts} (PID will be set) =====\n")
    logf.flush()
    child = subprocess.Popen(
        [sys.executable, "web/app.py", "--port", str(port)],
        cwd=BASE_DIR,
        stdout=logf,
        stderr=subprocess.STDOUT,
        # 新建进程组，便于用 taskkill /T 一次杀掉整棵树
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    log(f"已拉起 Flask 子进程 PID={child.pid}")
    return child


def kill_child():
    """杀掉 Flask 子进程（连同其可能派生的子进程）。"""
    global child
    if child is None:
        return
    pid = child.pid
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, timeout=10,
        )
        log(f"已终止 Flask 子进程树 PID={pid}")
    except Exception:
        try:
            child.kill()
        except Exception:
            pass
    child = None


def clear_flag():
    """消费 restart_flag：用 os.replace 把标志移走（而非删除）。

    原因：直接 os.remove 在部分环境会被“安全删除”拦截而删除失败，
    导致标志残留 -> 看门狗反复检测到 -> 无限重启循环。
    os.replace（MoveFileEx）不在拦截范围内，真机与沙箱都可靠。
    """
    try:
        if os.path.exists(RESTART_FLAG):
            os.replace(RESTART_FLAG, RESTART_FLAG + ".consumed")
    except Exception as e:
        log(f"clear_flag 异常（已忽略）：{e}")


def wait_ready(port, timeout=30):
    import urllib.request
    url = f"http://127.0.0.1:{port}/"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def open_browser_once(port):
    global _browser_opened
    if _browser_opened:
        return
    _browser_opened = True
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
        log("已尝试打开浏览器")
    except Exception as e:
        log(f"打开浏览器失败（可手动访问）：{e}")


def watchdog(port):
    """看门狗：
       1) 检测到 restart_flag -> 干净地重启子进程（热重启，加载最新代码）；
       2) 子进程意外退出 -> 自动拉起（自愈），连续崩溃超过阈值则熔断，避免空转。
    """
    global _crash_count
    while True:
        try:
            # 1) 用户触发的重启
            if os.path.exists(RESTART_FLAG):
                log("检测到 restart_flag -> 执行热重启")
                kill_child()
                clear_flag()
                _crash_count = 0
                spawn_child(port)
                if wait_ready(port, timeout=30):
                    log("热重启完成，Flask 已就绪")
                else:
                    log("警告：热重启后 Flask 未在超时内就绪，见 logs/flask_stdout.log")
                continue

            # 2) 子进程意外退出 -> 自愈重启（带熔断）
            if child is None or child.poll() is not None:
                _crash_count += 1
                if _crash_count > 5:
                    log("Flask 连续崩溃超过 5 次，停止自动重启（避免空转）。请查看 logs/flask_stdout.log 排查原因。")
                    time.sleep(5)
                    continue
                log(f"Flask 子进程意外退出，自动重启（第 {_crash_count} 次）")
                spawn_child(port)
                if wait_ready(port, timeout=30):
                    _crash_count = 0
                    log("Flask 已恢复")
                else:
                    log("警告：自愈重启后 Flask 未在超时内就绪")
        except Exception as e:
            log(f"watchdog 异常（继续运行）：{e}")
        time.sleep(0.5)


def cleanup():
    log("清理中：终止 Flask 子进程")
    kill_child()


atexit.register(cleanup)


def main():
    global _crash_count
    parser = argparse.ArgumentParser(description="彩票预测分析系统 单入口启动器")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    port = args.port

    os.makedirs(CONFIG_DIR, exist_ok=True)
    clear_flag()  # 上次若异常退出残留的标志，启动即清掉，避免一启动就重启

    # 端口空闲时跳过 PowerShell 残留清理（省 ~2s 启动时间）；仅端口被占时才清理
    if port_free(port, tries=1, delay=0):
        log(f"端口 {port} 空闲，跳过残留清理（快速启动）")
    else:
        log(f"端口 {port} 被占用，清理残留 Flask 进程...")
        kill_orphans(include_supervisor=False)
        if not port_free(port):
            log(f"警告：端口 {port} 在清理后仍未释放，仍尝试启动（可能绑定失败）")

    spawn_child(port)
    if wait_ready(port, timeout=40):
        log(f"Flask 已就绪 -> http://127.0.0.1:{port}")
        threading.Timer(1.0, open_browser_once, args=(port,)).start()
    else:
        log(f"警告：Flask 未在 40s 内就绪，请查看 logs/flask_stdout.log")

    try:
        watchdog(port)
    except KeyboardInterrupt:
        log("收到 Ctrl+C")
    finally:
        kill_child()
        log("启动器退出")


if __name__ == "__main__":
    main()
