# -*- coding: utf-8 -*-
"""探针：是否有农历库可用于推算未来春节日期（只读）。"""
import sys

mods = ["lunardate", "zhdate", "lunarcalendar", "cnlunar", "borax", "sxtwl", "lunar_python"]
for m in mods:
    try:
        mod = __import__(m)
        print(f"[OK]   {m}  {getattr(mod, '__version__', '')}")
    except Exception as e:
        print(f"[MISS] {m}  ({type(e).__name__})")

print()
try:
    import lunardate
    for y in (2027, 2028, 2029, 2030):
        d = lunardate.LunarDate(y, 1, 1).toSolarDate()
        print(f"lunardate {y} 春节 = {d}")
except Exception as e:
    print("lunardate 不可用:", e)

print()
try:
    from chinese_calendar import constants as c
    print("cc constants 覆盖年份:", getattr(c, "holidays", None) and "有 holidays 常量")
except Exception as e:
    print("cc constants 读取失败:", e)
