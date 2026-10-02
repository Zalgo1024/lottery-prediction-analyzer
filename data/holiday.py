"""
节假日/休市模块
处理春节/国庆停售对开奖日期的影响，支持手工节假日覆盖（config.HOLIDAY_OVERRIDE）

=== 休市口径（2026-09-21 重写）===
彩票市场休市 ≠ 法定节假日。真身是财政部/民政部/体育总局每年发布的
「彩票市场休市公告」，只有**春节**和**国庆**两个窗口：

    · 元旦、清明、五一、端午、中秋 —— 全部照常开奖
      （用本地 6 彩种 2004-11-14 ~ 2026-09-20 全部真实开奖数据实测：
        397 个法定节假日当天市场正常开奖，例 2026-01-01、2026-04-04 都有开奖记录）
    · 春节窗口逐年不同（2005-2020 为 7~8 天，2021 年起为 10 天）
    · 国庆自 2019 年首次休市（7 天），2020 年起固定 10-01 ~ 10-04
    · 2020-01-22 ~ 2020-03-10 为新冠疫情全国停售（49 天），真实特例

历史精确窗口写在 config.MARKET_CLOSURE（由真实开奖数据反推，测试锁定一致性）；
未覆盖的年份走春节日期表 + 近年规律推算兜底。

⚠️ 不再使用 chinese_calendar 的「法定节假日」概念：它把清明/五一/端午/中秋/元旦
   也算成休市，与事实不符（实测 85 期开奖日被误判为休市），且覆盖年限有限。
"""

import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import List, Optional

from config import (
    HOLIDAY_OVERRIDE,
    LOTTERY_CONFIG,
    MARKET_CLOSURE,
    NATIONAL_DAY_CLOSURE_FROM,
    NATIONAL_DAY_CLOSURE_RULE,
    SPRING_FESTIVAL_DATES,
)

logger = logging.getLogger(__name__)

# 导入 chinese_calendar（仅作未来年份的春节兜底，不再当"法定节假日"用）
try:
    import chinese_calendar as cc

    _HAS_CC = True
except ImportError:
    _HAS_CC = False
    logger.warning("chinese_calendar 未安装，未来年份的春节兜底将退化为不判休市")

# 表内年份 = 精确值（不走推算）；更晚的年份才走兜底
_TABLE_MAX_YEAR = max(MARKET_CLOSURE) if MARKET_CLOSURE else 0
_TABLE_MIN_YEAR = min(MARKET_CLOSURE) if MARKET_CLOSURE else 0
# 春节休市窗口：正月初一前 3 天 ~ 后 7 天（共 11 天，覆盖近 6 年观测的 10~11 天）
_SPRING_BEFORE = 3
_SPRING_AFTER = 7

# 开奖日"已开奖"判定时刻（沿用 web/utils 既有口径，2026-10-02 统一收编到本模块）
DAILY_CUTOFF = (21, 0)     # 每日彩种：当日 21:00 后视为已开奖
WEEKLY_CUTOFF = (21, 30)   # 非每日彩种：当日 21:30 后视为已开奖
_MAX_BACKTRACK = 400       # 回溯上限（覆盖 2020 疫情 49 天休市并留足余量）


def _in_spans(d: date, spans) -> bool:
    """d 是否落在 [(月, 日, 天数), ...] 描述的任一窗口内（按 d.year 解析）"""
    for month, day, days in spans:
        try:
            start = date(d.year, month, day)
        except ValueError:  # pragma: no cover - 非法月日配置
            continue
        if start <= d <= start + timedelta(days=days - 1):
            return True
    return False


def _is_override_holiday(d: date) -> bool:
    """检查是否在手工覆盖的休市范围内"""
    return _in_spans(d, HOLIDAY_OVERRIDE.get(d.year, []))


@lru_cache(maxsize=32)
def _cc_spring_span(year: int):
    """用 chinese_calendar 找该年春节假期区间（返回 (首日, 末日)，无数据返回 None）

    仅作 SPRING_FESTIVAL_DATES 未收录时的二级兜底。cc 只覆盖到 2026。
    """
    if not _HAS_CC:
        return None
    days = []
    d = date(year, 1, 1)
    end = date(year, 3, 1)
    while d <= end:
        try:
            is_off, name = cc.get_holiday_detail(d)
        except Exception:
            is_off, name = False, None
        if is_off and name == "Spring Festival":
            days.append(d)
        d += timedelta(days=1)
    if not days:
        return None
    return (min(days), max(days))


def _projected_holiday(d: date) -> bool:
    """MARKET_CLOSURE 未覆盖年份（2027+）的休市推算兜底。

    · 国庆：2020 年起稳定为 10-01 ~ 10-04
    · 春节：正月初一（查 SPRING_FESTIVAL_DATES）前 3 天 ~ 后 7 天
    · 都查不到 → 不判休市（保守：漏判只会让开奖日历多算一天，不会误杀战绩）
    """
    if d.year <= _TABLE_MAX_YEAR:
        return False  # 早于表起点的年份：无数据覆盖，不判休市

    if d.year >= NATIONAL_DAY_CLOSURE_FROM and _in_spans(d, [NATIONAL_DAY_CLOSURE_RULE]):
        return True

    md = SPRING_FESTIVAL_DATES.get(d.year)
    if md:
        day1 = date(d.year, md[0], md[1])
        return day1 - timedelta(days=_SPRING_BEFORE) <= d <= day1 + timedelta(days=_SPRING_AFTER)

    span = _cc_spring_span(d.year)
    if span:
        return span[0] - timedelta(days=_SPRING_BEFORE) <= d <= span[1] + timedelta(days=_SPRING_AFTER)

    return False


def is_holiday(d: date) -> bool:
    """
    判断某天是否为彩票休市日（全市场统一）

    优先级：手工覆盖 > 精确休市表 > 未来年份推算兜底
    """
    # ① 手工覆盖优先
    if _is_override_holiday(d):
        return True

    # ② 精确休市表（2005~2026，由真实开奖数据反推）
    if d.year in MARKET_CLOSURE:
        return _in_spans(d, MARKET_CLOSURE[d.year])

    # ③ 表外年份（未来）→ 春节/国庆规律推算
    return _projected_holiday(d)


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


def expected_latest_draw_date(lottery_type: str, now: Optional[datetime] = None) -> date:
    """最近一个「按日历应该已经开奖」的日期（休市日不算）。

    · 每日彩种：当日已过 21:00 → 今天，否则昨天；再向前跳过所有休市日。
    · 非每日彩种：当日已过 21:30 → 今天，否则昨天；再向前找最近的合法开奖日。
    · 休市期间：返回休市前最后一个开奖日（2026-10-02 → 每日 09-30 / 双色球 09-29）。
    · 找不到（理论不会发生）→ 返回回溯到底的日期，绝不抛异常。
    """
    if now is None:
        now = datetime.now()
    cfg = LOTTERY_CONFIG.get(lottery_type) or {}
    is_daily = len(set(cfg.get("draw_days", []) or [])) == 7
    h, m = DAILY_CUTOFF if is_daily else WEEKLY_CUTOFF
    cutoff = now.replace(hour=h, minute=m, second=0, microsecond=0)
    cand = now.date() if now >= cutoff else now.date() - timedelta(days=1)
    for _ in range(_MAX_BACKTRACK):
        if is_draw_day(lottery_type, cand):
            return cand
        cand -= timedelta(days=1)
    return cand


def closure_window(d: Optional[date] = None) -> Optional[dict]:
    """返回 d 所在的休市窗口；不在休市期返回 None。

    {"start": date, "end": date, "days": int, "name": "春节"/"国庆"/"休市",
     "resume": date}    # resume = end + 1 天（市场恢复日；各彩种按自己开奖日历再落）

    用 is_holiday 向两侧扩张，自动覆盖 HOLIDAY_OVERRIDE / MARKET_CLOSURE / 未来推算三层。
    """
    d = d or date.today()
    if not is_holiday(d):
        return None
    start = end = d
    for _ in range(_MAX_BACKTRACK):
        prev = start - timedelta(days=1)
        if not is_holiday(prev):
            break
        start = prev
    for _ in range(_MAX_BACKTRACK):
        nxt = end + timedelta(days=1)
        if not is_holiday(nxt):
            break
        end = nxt
    name = "春节" if start.month in (1, 2) else ("国庆" if start.month in (9, 10) else "休市")
    return {"start": start, "end": end, "days": (end - start).days + 1,
            "name": name, "resume": end + timedelta(days=1)}


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
