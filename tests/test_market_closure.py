# -*- coding: utf-8 -*-
"""休市日历（config.MARKET_CLOSURE）与真实开奖数据的强一致性锁。

背景（2026-09-21）：旧口径把 chinese_calendar 的「法定节假日」当休市，
但彩票市场实际只在春节/国庆休市，导致 85 期开奖日被判成休市 →
predict_target 的「预测日期」被推到下一期 → 结算时真预测被误标为「训练」。

本测试用本地真实开奖数据反向锁死休市表：
  ① 真实有开奖的日子，绝不能被判为休市（正向）；
  ② 3 个「天天开奖」彩种同时缺记录的日子，必须被判为休市（反向）。

数据增长后若出现新的休市窗口而未补进表里，本测试会 fail —— 这是期望行为，
请把新窗口补进 config.MARKET_CLOSURE。
"""

from datetime import date, timedelta

import pytest

from config import MARKET_CLOSURE, SPRING_FESTIVAL_DATES
from data.holiday import is_holiday
from data.loader import load_lottery

# 每天开奖的彩种：它们同时缺记录 = 全市场休市（可排除单彩种数据缺失）
DAILY_LOTS = ["排列3", "排列5", "福彩3D"]


@pytest.fixture(scope="module")
def daily_actual():
    """{彩种: 实际开奖日集合}"""
    out = {}
    for lot in DAILY_LOTS:
        recs = [r for r in load_lottery(lot).records if r.开奖日期]
        out[lot] = {r.开奖日期 for r in recs}
    return out


def test_no_draw_day_is_marked_closed(daily_actual):
    """① 正向：任何真实开奖日都不能被判为休市（旧口径的 85 期判错在此归零）。"""
    bad = []
    for lot, days in daily_actual.items():
        for d in days:
            if d.year in MARKET_CLOSURE and is_holiday(d):
                bad.append((lot, str(d)))
    assert bad == [], f"{len(bad)} 个真实开奖日被判为休市（前 10 条）: {sorted(bad)[:10]}"


def test_all_missing_days_are_marked_closed(daily_actual):
    """② 反向：3 个天天开彩种同时缺记录的日子，必须被判为休市。"""
    lo = max(min(daily_actual[l]) for l in DAILY_LOTS)
    hi = min(max(daily_actual[l]) for l in DAILY_LOTS)
    missing, d = [], lo
    while d <= hi:
        if all(d not in daily_actual[l] for l in DAILY_LOTS):
            missing.append(d)
        d += timedelta(days=1)

    bad = [str(x) for x in missing if not is_holiday(x)]
    assert bad == [], f"{len(bad)} 个全市场停售日未被判为休市（前 10 条）: {bad[:10]}"

    # 全区间内，被判休市的日子必须都在这场「同时缺记录」集合里（无凭空休市）
    missing_set = set(missing)
    extra, d = [], lo
    while d <= hi:
        if is_holiday(d) and d not in missing_set:
            extra.append(str(d))
        d += timedelta(days=1)
    assert extra == [], f"凭空判休市（当天实际有开奖）: {extra[:10]}"


def test_table_shape_is_sane():
    """表结构自检：年份连续、窗口天数合理、无空年份。"""
    years = sorted(MARKET_CLOSURE)
    assert years[0] == 2005 and years[-1] >= 2026
    for y in years:
        spans = MARKET_CLOSURE[y]
        assert spans, f"{y} 年窗口为空"
        for month, day, days in spans:
            assert month in (1, 2, 9, 10), f"{y} 年出现了非春节/国庆窗口: ({month},{day},{days})"
            assert 1 <= days <= 60, f"{y} 年窗口天数异常: {days}"


def test_spring_festival_dates_are_monotone_and_in_season():
    """未来春节日期表自检：落在 1/21 ~ 2/21（春节公历区间），且逐年递增。"""
    items = sorted(SPRING_FESTIVAL_DATES.items())
    prev = None
    for y, (m, d) in items:
        ok = (m == 1 and 21 <= d <= 31) or (m == 2 and 1 <= d <= 21)
        assert ok, f"{y} 春节日期不合理: {m}-{d}"
        cur = date(y, m, d)
        if prev is not None:
            gap = (cur - prev).days
            assert 320 <= gap <= 400, f"{y} 与上一年的春节间隔异常: {gap} 天"
        prev = cur
