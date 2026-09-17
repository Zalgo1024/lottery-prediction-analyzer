"""
构建脚本：将 Flask Web 应用打包为单 exe
运行：python build_exe.py
"""

import sys
import os
import shutil

# 提高递归限制（PyInstaller 处理 scipy/sklearn 时需要）
sys.setrecursionlimit(10000)

# 清理旧构建
for d in ['build', 'dist']:
    if os.path.exists(d):
        shutil.rmtree(d)
for f in ['LotteryAnalyzer.spec']:
    if os.path.exists(f):
        os.remove(f)

# 改用 PyInstaller 命令行
os.system(
    'pyinstaller --noconfirm'
    ' --name LotteryAnalyzer'
    ' --add-data "web/templates;web/templates"'
    ' --add-data "web/static;web/static"'
    ' --hidden-import web.routes.dashboard'
    ' --hidden-import web.routes.stats'
    ' --hidden-import web.routes.pipeline'
    ' --hidden-import web.routes.train'
    ' --hidden-import web.routes.predict'
    ' --hidden-import web.routes.anomaly'
    ' --hidden-import web.routes.history'
    ' --hidden-import web.routes.feedback'
    ' --hidden-import web.routes.rolling'
    ' --hidden-import web.routes.records'
    ' --hidden-import web.routes.ev'
    ' --hidden-import pipeline.step6_ev'
    ' --hidden-import data.fetcher'
    ' --hidden-import data.records'
    ' --hidden-import data.feedback'
    ' --hidden-import prediction.rule_strategy'
    ' --hidden-import prediction.optimizer'
    ' --hidden-import train.rolling_trainer'
    ' --hidden-import sklearn.linear_model'
    ' --hidden-import sklearn.ensemble'
    ' --hidden-import sklearn.preprocessing'
    ' --hidden-import scipy.stats'
    ' --exclude-module tkinter'
    ' --exclude-module PyQt5'
    ' --exclude-module tensorflow'
    ' --exclude-module torch'
    ' --exclude-module transformers'
    ' --exclude-module keras'
    ' --exclude-module sympy'
    ' --exclude-module pyarrow'
    ' --exclude-module grpc'
    ' --exclude-module zmq'
    ' --exclude-module pygments'
    ' --exclude-module lxml'
    ' --exclude-module h5py'
    ' --exclude-module jedi'
    ' --exclude-module parso'
    ' --exclude-module IPython'
    ' --exclude-module nbformat'
    ' --exclude-module jsonschema'
    ' --exclude-module PIL'
    ' --console'
    ' launcher.py'
)
