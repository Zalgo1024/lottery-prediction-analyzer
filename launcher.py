"""
彩票预测分析系统 Web 启动器
双击此文件（或打包后的 exe）即可启动 Web 服务
自动在浏览器中打开 http://127.0.0.1:5000
"""

import os
import sys
import webbrowser
from threading import Timer

# 确保能找到项目模块（无论是源码运行还是 PyInstaller 打包）
if getattr(sys, 'frozen', False):
    # PyInstaller 打包后，_MEIPASS 是解压临时目录
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 将项目根目录加入 Python 路径
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)  # 切换到项目根目录，确保 CSV 文件可访问


def open_browser(port):
    """延迟 1.5 秒后自动打开浏览器"""
    def _open():
        url = f"http://127.0.0.1:{port}"
        webbrowser.open(url)
        print(f"🌐 浏览器已打开: {url}")
    Timer(1.5, _open).start()


def main():
    port = 5000

    print("=" * 60)
    print("  🎯  彩票预测分析系统  —  Web 服务")
    print("=" * 60)
    print(f"  📂 工作目录: {BASE_DIR}")
    print(f"  🌐 访问地址: http://127.0.0.1:{port}")
    print(f"  🚀 正在启动...")
    print("=" * 60)
    print("  关闭本窗口即可停止服务")
    print()

    # 自动打开浏览器
    open_browser(port)

    # 启动 Flask
    from web.app import app
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
