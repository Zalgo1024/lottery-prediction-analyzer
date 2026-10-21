# -*- coding: utf-8 -*-
"""摸清「开奖后回测·训练」记录到底有多少、从哪来（只读）。"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"E:\707")
from data.feedback import FEEDBACK_DIR, load_feedback_history  # noqa: E402

print("FEEDBACK_DIR =", FEEDBACK_DIR)
print("存在:", FEEDBACK_DIR.exists())
files = sorted(FEEDBACK_DIR.glob("*"))
for f in files[:30]:
    print("  ", f.name, f.stat().st_size)
print()

for lot in ["双色球", "福彩3D"]:
    hist = load_feedback_history(lot)
    v = [r for r in hist if r.get("valid_prediction", True)]
    t = [r for r in hist if not r.get("valid_prediction", True)]
    print(f"=== {lot}: 总 {len(hist)} | 真预测 {len(v)} | 训练/回测 {len(t)}")
    print("  训练记录 来源分布:", Counter(r.get("来源") for r in t))
    print("  训练记录 记录类型分布:", Counter(r.get("记录类型") for r in t))
    print("  训练记录 批次来源分布:", Counter(r.get("来源批次") for r in t))
    for r in t[-3:]:
        keys = ["期号", "预测日期", "开奖日期", "来源", "记录类型", "批次号", "策略", "中奖等级", "中奖玩法"]
        print("   -", {k: r.get(k) for k in keys if k in r})
    print()
