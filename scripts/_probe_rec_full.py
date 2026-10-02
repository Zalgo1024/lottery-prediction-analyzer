# -*- coding: utf-8 -*-
"""逐字段 dump 双色球 26109 / 福彩3D 2026253 的反馈记录（只读）。"""
import csv
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, r"E:\707")
from data.feedback import load_feedback_history  # noqa: E402

for lot, iss in [("双色球", 26109), ("福彩3D", 2026253)]:
    print("=" * 78)
    hist = load_feedback_history(lot)
    recs = [r for r in hist if str(r.get("期号")) == str(iss)]
    print(f"{lot} 期号 {iss}: {len(recs)} 条")
    keys = ["期号", "预测日期", "开奖日期", "来源", "记录类型", "valid_prediction",
            "批次号", "批次时间", "批次规模", "压缩前注数", "批次内序号", "档位", "策略"]
    for r in recs[:3]:
        print("  -", json.dumps({k: r.get(k) for k in keys if k in r}, ensure_ascii=False))
    if recs:
        print("  全部键:", sorted(recs[0].keys()))
    print()

# 本地最新开奖（CSV 编码探测）
print("=" * 78)
for f in sorted(glob.glob(r"E:\707\lottery_data\*.csv")):
    name = Path(f).name
    for enc in ("utf-8-sig", "gbk"):
        try:
            rows = list(csv.DictReader(open(f, encoding=enc)))
            cols = list(rows[0].keys())[:3]
            last = rows[-1]
            print(f"{name:34s} [{enc}] 行数={len(rows):5d} 列={cols} 末期={ {k: last[k] for k in cols} }")
            break
        except Exception as e:
            print(f"{name:34s} [{enc}] 失败: {type(e).__name__}")
