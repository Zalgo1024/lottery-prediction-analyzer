# -*- coding: utf-8 -*-
"""端到端自洽性验证：修好休市日历后，6 彩种的预测目标期推算是否合理（只读）。

断言：predict_target 给出的开奖日必须
  ① 在未来（> 本地最近开奖日）
  ② 落在该彩种规定的星期几
  ③ 不是休市日
"""
import sys
from datetime import date

sys.path.insert(0, r"E:\707")
from config import LOTTERY_CONFIG  # noqa: E402
from data.feedback import predict_target  # noqa: E402
from data.holiday import is_draw_day, is_holiday  # noqa: E402
from data.loader import load_lottery  # noqa: E402

LOTS = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]
WD = "一二三四五六日"
ok = True

print(f"{'彩种':8s} {'目标期号':>10s}  {'推算开奖日':>12s}  {'周几':>4s}  判定")
print("-" * 64)
for lot in LOTS:
    issue, draw = predict_target(lot)
    recs = [r for r in load_lottery(lot).records if r.开奖日期]
    last = max(r.开奖日期 for r in recs)
    cfg = LOTTERY_CONFIG[lot]
    checks = []
    if draw is None:
        checks.append("开奖日为 None")
    else:
        if not draw > last:
            checks.append(f"不在未来(本地最近 {last})")
        if draw.weekday() not in cfg["draw_days"]:
            checks.append("星期几不符")
        if is_holiday(draw):
            checks.append("被判休市")
        if not is_draw_day(lot, draw):
            checks.append("is_draw_day=False")
    wd = WD[draw.weekday()] if draw else "-"
    status = "OK" if not checks else "✗ " + "; ".join(checks)
    if checks:
        ok = False
    print(f"{lot:8s} {issue:>10}  {str(draw):>12s}  周{wd:>2s}  {status}")

print()
print("=== 关键日期休市判定 ===")
for d in [date(2026, 9, 20), date(2026, 9, 21), date(2026, 10, 1),
          date(2026, 12, 31), date(2027, 2, 6), date(2027, 10, 2)]:
    print(f"  {d} 周{WD[d.weekday()]}  休市={is_holiday(d)}")

print()
print("结果:", "全部自洽 ✅" if ok else "存在不自洽项 ❌")
sys.exit(0 if ok else 1)
