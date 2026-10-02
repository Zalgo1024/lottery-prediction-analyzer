# -*- coding: utf-8 -*-
"""验证：开奖日历 is_draw_day 是否把 09-20（本地 CSV 实证有开奖）判成非开奖日。"""
import sys
from datetime import date

sys.path.insert(0, r"E:\707")
from config import LOTTERY_CONFIG  # noqa: E402
from data.holiday import is_draw_day, is_holiday, next_draw_date  # noqa: E402

try:
    import chinese_calendar as cc
    print("chinese_calendar 版本:", getattr(cc, "__version__", "?"))
except Exception as e:
    cc = None
    print("chinese_calendar 不可用:", e)

LOTS = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]
KNOWN = [date(2026, 9, 20), date(2026, 9, 19), date(2026, 9, 18), date(2026, 9, 17),
         date(2026, 9, 15), date(2026, 9, 13), date(2026, 9, 6)]

print()
print("draw_days 配置:", {k: v.get("draw_days") for k, v in LOTTERY_CONFIG.items()})
print()
for d in KNOWN:
    wd = "一二三四五六日"[d.weekday()]
    named = None
    if cc is not None:
        try:
            named = cc.get_holiday_detail(d)
        except Exception as e:
            named = f"ERR {type(e).__name__}"
    print(f"{d} 周{wd}  is_holiday={is_holiday(d)}  cc.get_holiday_detail={named}")

print()
for lot in LOTS:
    row = "  ".join(f"{d.strftime('%m-%d')}:{'开' if is_draw_day(lot, d) else '×'}" for d in KNOWN)
    print(f"{lot:6s} {row}")
print()
for lot in LOTS:
    print(f"{lot:6s} next_draw_date(from 09-18) = {next_draw_date(lot, date(2026, 9, 18))}"
          f"  | from 09-19 = {next_draw_date(lot, date(2026, 9, 19))}")
