# -*- coding: utf-8 -*-
"""用本地真实开奖数据当基准，核对开奖日历（只读，不写任何东西）。

基准：CSV 里每一期都有开奖日期，那天必然是该彩种的开奖日。
所以 is_draw_day(该日) 必须为 True —— 统计不满足的条数（修复前 vs 修复后）。
"""
import sys
from datetime import date

sys.path.insert(0, r"E:\707")
from data.holiday import is_holiday  # noqa: E402
from data.loader import load_lottery  # noqa: E402
import data.holiday as h  # noqa: E402

try:
    import chinese_calendar as cc
except Exception:
    cc = None

LOTS = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]
TAIL = 300


def _old_is_holiday(d):
    """修复前的口径：只看节日名（忽略 is_off）。"""
    if h._is_override_holiday(d):
        return True
    if h._HAS_CC and cc is not None:
        try:
            _, name = cc.get_holiday_detail(d)
            return name is not None
        except Exception:
            return False
    return False


def _is_draw_day_with(old_holiday, lottery, d):
    cfg = h.LOTTERY_CONFIG.get(lottery)
    if cfg is None or d.weekday() not in cfg["draw_days"]:
        return False
    return not old_holiday(d)


total_new = total_old = 0
for lot in LOTS:
    data = load_lottery(lot)
    recs = [r for r in data.records if r.开奖日期]
    recs.sort(key=lambda r: r.期号 or 0)
    tail = recs[-TAIL:]
    bad_new, bad_old, samples = 0, 0, []
    for r in tail:
        d = r.开奖日期
        if not _is_draw_day_with(is_holiday, lot, d):
            bad_new += 1
        if not _is_draw_day_with(_old_is_holiday, lot, d):
            bad_old += 1
            samples.append((r.期号, str(d)))
    total_new += bad_new
    total_old += bad_old
    print(f"{lot:6s} 近 {len(tail):3d} 期：修复后判错 {bad_new:3d} 期 | 修复前判错 {bad_old:3d} 期")
    if samples:
        print(f"        修复前误判样例（期号, 开奖日）: {samples[:6]}")

print()
print(f"合计：修复后 {total_new} 期判错 | 修复前 {total_old} 期判错")
