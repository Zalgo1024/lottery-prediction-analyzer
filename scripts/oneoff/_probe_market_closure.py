# -*- coding: utf-8 -*-
"""反推真实休市窗口（只读）。

依据：排列3/排列5/福彩3D 是「每天开奖」的彩种，本地 CSV 里若有某天缺记录，
那这天就极可能是全市场休市日。用「3 个天天开彩种同时缺」作强判据，
排除单彩种数据缺失造成的误判。

输出：按年聚合的休市窗口清单，供写进 config.HOLIDAY_OVERRIDE。
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, r"E:\707")
from data.loader import load_lottery  # noqa: E402

DAILY_LOTS = ["排列3", "排列5", "福彩3D"]   # 每天开奖
ALL_LOTS = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]

# 1) 收集每个彩种的实际开奖日集合
actual = {}
span = {}
for lot in ALL_LOTS:
    data = load_lottery(lot)
    recs = [r for r in data.records if r.开奖日期]
    days = {r.开奖日期 for r in recs}
    actual[lot] = days
    if days:
        span[lot] = (min(days), max(days), len(days))

print("=== 各彩种开奖日跨度 ===")
for lot in ALL_LOTS:
    if lot in span:
        a, b, n = span[lot]
        print(f"  {lot:6s} {a} ~ {b}   共 {n} 个开奖日")
print()

# 2) 以「天天开彩种」的并集为全市场时间轴，找出同时缺失的日子
lo = max(span[l][0] for l in DAILY_LOTS)
hi = min(span[l][1] for l in DAILY_LOTS)
print(f"=== 全市场判定区间（3 个天天开彩种的交集）: {lo} ~ {hi} ===")
print()

missing = []          # 3 个天天开彩种全部缺失的日子
d = lo
while d <= hi:
    cnt = sum(1 for l in DAILY_LOTS if d not in actual[l])
    if cnt == len(DAILY_LOTS):
        missing.append(d)
    d += timedelta(days=1)

print(f"=== 3 个天天开彩种「同时缺记录」的日子共 {len(missing)} 天 ===")
# 聚合成连续窗口
windows = []
if missing:
    start = prev = missing[0]
    for x in missing[1:]:
        if (x - prev).days == 1:
            prev = x
        else:
            windows.append((start, prev))
            start = prev = x
    windows.append((start, prev))

print()
print("=== 聚合后的窗口（含窗口内其它彩种情况）===")
for s, e in windows:
    n = (e - s).days + 1
    # 窗口内各彩种实际开奖数
    detail = ", ".join(f"{l}:{sum(1 for x in (s + timedelta(i) for i in range(n)) if x in actual[l])}"
                       for l in ALL_LOTS)
    print(f"  {s} ~ {e}  ({n:2d} 天)   窗口内各彩种开奖数: {detail}")

print()
print("=== 按年聚合（供 HOLIDAY_OVERRIDE 使用）===")
by_year = {}
for s, e in windows:
    by_year.setdefault(s.year, []).append((s, e))
for y in sorted(by_year):
    items = []
    for s, e in by_year[y]:
        n = (e - s).days + 1
        items.append(f"({s.month:2d}, {s.day:2d}, {n:2d})  # {s} ~ {e}")
    print(f"  {y}: [")
    for it in items:
        print(f"      {it},")
    print("  ],")

# 3) 反查：法定节假日但实际照常开奖的日子（验证「只有春节/国庆休市」）
try:
    import chinese_calendar as cc
    print()
    print("=== 法定节假日但市场照常开奖的日子（证明普通法定节假日不休市）===")
    samples = []
    d = lo
    while d <= hi:
        try:
            is_off, name = cc.get_holiday_detail(d)
        except Exception:
            is_off, name = False, None
        if is_off and name is not None and d in actual[DAILY_LOTS[0]]:
            samples.append((d, name))
        d += timedelta(days=1)
    print(f"  共 {len(samples)} 天：")
    for d0, nm in samples[:40]:
        print(f"    {d0}  {nm}")
    if len(samples) > 40:
        print(f"    ... 另有 {len(samples) - 40} 天")
except Exception as ex:
    print("chinese_calendar 不可用:", ex)
