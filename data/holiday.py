"""
节假日/休市模块
封装 chinese_calendar，处理春节/国庆停售对开奖日期的影响
支持手动节假日覆盖（config.HOLIDAY_OVERRIDE）

核心逻辑：彩票在法定节假日停售，开奖顺延到下一个非节假日的开奖日
"""

import logging
from datetime import date, timedelta
from typing import List, Optional

from config import LOTTERY_CONFIG, HOLIDAY_OVERRIDE

logger = logging.getLogger(__name__)

# 导入 chinese_calendar
try:
    import chinese_calendar as cc

    _HAS_CC = True
except ImportError:
    _HAS_CC = False
    logger.warning("chinese_calendar 未安装，节假日判断降级为仅按周几过滤")


def _is_override_holiday(d: date) -> bool:
    """检查是否在手工覆盖的休市范围内"""
    overrides = HOLIDAY_OVERRIDE.get(d.year, [])
    for month, day, days in overrides:
        start = date(d.year, month, day)
        end = start + timedelta(days=days - 1)
        if start <= d <= end:
            return True
    return False


def is_holiday(d: date) -> bool:
    """
    判断某天是否为彩票休市日
    彩票停售 = 法定节假日（含春节、国庆及连休 span），普通周末照常开奖！

    ⚠️ 历史教训：chinese_calendar.is_holiday() 会把所有普通周末判为 True
    （休息日 ≠ 法定节假日），曾导致 is_draw_day 永远排除周日/周六 →
    双色球/七星彩周日、大乐透周六的开奖日历失效，「预测日期」被推到下一期
    （2026-09-06 实锤：26103 期预测日期被写成 9/8）。
    修正：用 get_holiday_detail 的法定节日名区分——有名字=法定节假日，
    None=普通周末/工作日（照常开奖）。
    """
    # 手工覆盖优先
    if _is_override_holiday(d):
        return True

    # chinese_calendar：仅法定节假日（节日名非 None）算休市
    if _HAS_CC:
        try:
            _, holiday_name = cc.get_holiday_detail(d)
            return holiday_name is not None
        except Exception:
            return False

    return False


def is_draw_day(lottery_type: str, d: date) -> bool:
    """
    判断某天是否为该彩票的开奖日
    需要同时满足：① 是规定的开奖星期几；② 不是休市日
    """
    cfg = LOTTERY_CONFIG.get(lottery_type)
    if cfg is None:
        return False

    # 检查星期几是否符合
    weekday = d.weekday()  # 0=周一…6=周日
    if weekday not in cfg["draw_days"]:
        return False

    # 检查是否休市
    if is_holiday(d):
        return False

    return True


def next_draw_date(lottery_type: str, from_date: Optional[date] = None) -> date:
    """
    获取指定日期之后的下一个开奖日
    如果 from_date 本身是开奖日，返回 from_date
    """
    if from_date is None:
        from_date = date.today()

    d = from_date
    for _ in range(366):
        if is_draw_day(lottery_type, d):
            return d
        d += timedelta(days=1)

    raise ValueError(f"在一年内未找到 {lottery_type} 的开奖日（从 {from_date} 起）")


def get_draw_dates_in_range(
    lottery_type: str, start: date, end: date
) -> List[date]:
    """获取日期范围内所有合法开奖日"""
    return [
        d
        for d in (start + timedelta(n) for n in range((end - start).days + 1))
        if is_draw_day(lottery_type, d)
    ]


def validate_date_continuity(
    lottery_type: str, dates: List[date]
) -> List[str]:
    """
    检查历史开奖日期的连续性
    返回跳过的期数说明（不是错误，只是说明）
    """
    messages = []
    for i in range(len(dates) - 1):
        curr = dates[i]
        nxt = dates[i + 1]
        gap = (curr - nxt).days  # 从新到旧排序
        if gap > 4:
            missed = get_draw_dates_in_range(lottery_type, nxt, curr)
            skipped = len(missed) - 2
            if skipped > 0:
                messages.append(
                    f"期号间跳过 {skipped} 个开奖日: {nxt} → {curr}（可能是节假日停售）"
                )
    return messages
