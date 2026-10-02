# -*- coding: utf-8 -*-
"""看 26109/2026253 这批 pending 的原始字段 + 本地最新开奖（只读）。"""
import sys

sys.path.insert(0, r"E:\707")
from data.feedback import load_pending  # noqa: E402
from data.holiday import next_draw_date  # noqa: E402
from datetime import date  # noqa: E402
import csv  # noqa: E402
import glob  # noqa: E402

for lot, iss in [("双色球", 26109), ("福彩3D", 2026253)]:
    print("=" * 70)
    print(lot, "pending 条数:", len(load_pending(lot)))
    for p in load_pending(lot):
        if str(p.get("目标期号")) == str(iss):
            keys = ["目标期号", "预测日期", "生成时间", "来源", "批次号", "状态", "训练标记"]
            print("  ", {k: p.get(k) for k in keys})
            print("    注数:", len(p.get("预测号码", [])), "| 压缩:", p.get("压缩", {}).get("压缩前注数"))
    print("  pending 里已有的目标期号:", [p.get("目标期号") for p in load_pending(lot)])
    print()

# 本地最新开奖
for pat in ["双色球", "福彩3D"]:
    for f in glob.glob(rf"E:\707\lottery_data\*{pat}*"):
        if f.endswith(".csv"):
            rows = list(csv.DictReader(open(f, encoding="utf-8-sig")))
            print(pat, "CSV:", f.split("\\")[-1], "行数:", len(rows), "| 表头:", list(rows[0].keys())[:6])
            for r in rows[-4:]:
                print("   末期:", {k: r[k] for k in list(r.keys())[:4]})
print()
for lot in ["双色球", "福彩3D", "大乐透", "排列3", "排列5", "七星彩"]:
    print(f"{lot}: next_draw_date(today=2026-09-21) =", next_draw_date(lot, date(2026, 9, 21)))
