# -*- coding: utf-8 -*-
"""复核：那几批「被判训练」的 pending，其 (目标期号, 预测日期) 放今天重算是否还会错配。（只读）"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, r"E:\707")
from data.feedback import _target_draw_date, _next_issue  # noqa: E402

CASES = [("双色球", 26109, "2026-09-22"), ("双色球", 26103, "2026-09-08"),
         ("福彩3D", 2026253, "2026-09-21"), ("福彩3D", 2026231, "2026-08-31"),
         ("排列5", 26253, "2026-09-21"), ("排列3", 26238, "2026-09-07"),
         ("七星彩", 26109, "2026-09-22"), ("大乐透", 26098, "2026-08-31")]

print("=== 重算 _target_draw_date（现在的代码口径）===")
for lot, iss, rec_date in CASES:
    now = _target_draw_date(lot, iss)
    flag = "✅一致" if str(now) == rec_date else "❌不一致"
    print(f"  {lot:5s} 期号={iss:<8} 记录里的预测日期={rec_date}  现在重算={now}  {flag}")

print()
print("=== 本地开奖数据里这些期的真实开奖日 ===")
FILES = {"双色球": r"E:\707\lottery_data\双色球历史数据_cleaned.csv",
         "福彩3D": r"E:\707\lottery_data\福彩3D历史数据.csv",
         "排列5": r"E:\707\lottery_data\排列5历史数据.csv",
         "七星彩": r"E:\707\lottery_data\七星彩历史数据.csv"}
WANT = {"双色球": ["26105", "26106", "26107", "26108", "26109", "26110"],
        "福彩3D": ["2026250", "2026251", "2026252", "2026253", "2026254"],
        "排列5": ["26251", "26252", "26253", "26254"],
        "七星彩": ["26107", "26108", "26109", "26110"]}
for lot, f in FILES.items():
    for enc in ("utf-8-sig", "gbk"):
        try:
            rows = list(csv.DictReader(open(f, encoding=enc)))
            break
        except Exception:
            rows = []
    dcol = "开奖日期" if rows and "开奖日期" in rows[0] else None
    hits = [r for r in rows if str(r.get("期号")) in WANT[lot]]
    print(f"  {lot}: ", [(r.get("期号"), r.get(dcol) if dcol else "?") for r in hits[:8]])
    if lot == "双色球" and not hits:
        print("     （该文件期号列可能不是 26xxx，表头:", list(rows[0].keys())[:5] if rows else None, "）")

print()
print("=== 各彩种当前 _next_issue ===")
for lot in ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]:
    print(f"  {lot}: {_next_issue(lot)}")
