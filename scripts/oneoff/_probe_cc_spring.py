# -*- coding: utf-8 -*-
"""探针：chinese_calendar 对春节的标注形态 + 数据覆盖年限（只读）。"""
import sys
from datetime import date, timedelta

sys.path.insert(0, r"E:\707")
try:
    import chinese_calendar as cc
    print("cc version:", getattr(cc, "__version__", "?"))
except Exception as e:
    print("cc 不可用:", e)
    raise SystemExit(1)


def spring_festival_span(year):
    days = []
    d = date(year, 1, 1)
    end = date(year, 3, 1)
    while d <= end:
        try:
            is_off, name = cc.get_holiday_detail(d)
        except Exception:
            is_off, name = False, None
        if is_off and name is not None:
            days.append((d, name))
        d += timedelta(days=1)
    return days


for y in (2024, 2025, 2026, 2027, 2028):
    got = spring_festival_span(y)
    print(f"\n--- {y} ---")
    if not got:
        print("  (无数据)")
        continue
    names = sorted({nm for _, nm in got})
    print("  节日名集合:", names)
    print("  明细:", [(str(d), nm) for d, nm in got])
