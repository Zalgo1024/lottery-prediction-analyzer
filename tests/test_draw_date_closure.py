"""休市日历接入开奖日推算（2026-10-02 国庆休市实测暴露的旁路 bug 回归）。

背景：web/utils 与 cli/scan/sales_model 曾各自用「weekday ∈ draw_days」推算，
不看 data.holiday 休市日历 → 国庆休市期间产出假滞后与落在休市日的「下一期」。
本文件锁死统一后的口径：真源 = data.holiday.expected_latest_draw_date / next_draw_date。
全部注入固定 now=，禁止依赖真实时钟。
"""

from datetime import date, datetime

import pytest

from data.holiday import (
    expected_latest_draw_date,
    is_holiday,
    next_draw_date,
)

LOTS_DAILY = ("排列3", "福彩3D", "排列5")
LOTS_WEEKLY = ("双色球", "大乐透", "七星彩")


# ---------------------------------------------------------------- data.holiday 真源
def test_next_draw_date_skips_national_day_closure():
    """从休市前最后开奖日的次日起推（调用方语义：latest+1），跳过 10-01~10-04 窗口。"""
    assert next_draw_date("双色球", date(2026, 9, 30)) == date(2026, 10, 6)
    assert next_draw_date("大乐透", date(2026, 10, 1)) == date(2026, 10, 5)
    assert next_draw_date("排列3", date(2026, 10, 1)) == date(2026, 10, 5)
    assert next_draw_date("七星彩", date(2026, 9, 30)) == date(2026, 10, 6)


def test_next_draw_date_from_closure_day():
    """休市日当天起推（from_date 含自身，但自身是休市日 → 落到恢复后首个开奖日）。"""
    assert next_draw_date("双色球", date(2026, 10, 2)) == date(2026, 10, 6)
    assert next_draw_date("大乐透", date(2026, 10, 2)) == date(2026, 10, 5)
    for lot in LOTS_DAILY:
        assert next_draw_date(lot, date(2026, 10, 2)) == date(2026, 10, 5)


def test_expected_on_closure_day_morning():
    """休市日上午：最近应已开奖日 = 休市前最后开奖日（不是昨天 10-01！）。"""
    now = datetime(2026, 10, 2, 10, 0)
    for lot in LOTS_DAILY:
        assert expected_latest_draw_date(lot, now) == date(2026, 9, 30), lot
    assert expected_latest_draw_date("双色球", now) == date(2026, 9, 29)
    assert expected_latest_draw_date("大乐透", now) == date(2026, 9, 30)
    assert expected_latest_draw_date("七星彩", now) == date(2026, 9, 29)


def test_expected_on_closure_day_evening():
    """休市日晚上：即使过了 21:00 也不前进——休市日不是开奖日。"""
    now = datetime(2026, 10, 2, 22, 0)
    for lot in LOTS_DAILY:
        assert expected_latest_draw_date(lot, now) == date(2026, 9, 30), lot
    assert expected_latest_draw_date("双色球", now) == date(2026, 9, 29)


def test_expected_on_eve_before_closure():
    """休市前夜 23:00（已过 21:00）：当日（09-30）就是最近应已开奖日。"""
    now = datetime(2026, 9, 30, 23, 0)
    for lot in LOTS_DAILY:
        assert expected_latest_draw_date(lot, now) == date(2026, 9, 30), lot
    assert expected_latest_draw_date("大乐透", now) == date(2026, 9, 30)
    assert expected_latest_draw_date("双色球", now) == date(2026, 9, 29)  # 周三非双色球日


def test_expected_on_resume_day_before_cutoff():
    """恢复日（10-05 周一）21:30 前：当晚还没开奖 → 仍是休市前最后开奖日。"""
    now = datetime(2026, 10, 5, 10, 0)
    for lot in LOTS_DAILY:
        assert expected_latest_draw_date(lot, now) == date(2026, 9, 30), lot
    assert expected_latest_draw_date("大乐透", now) == date(2026, 9, 30)  # 09-30 周三是大乐透开奖日
    assert expected_latest_draw_date("双色球", now) == date(2026, 9, 29)


def test_expected_on_resume_day_after_cutoff():
    """恢复日 21:30 后：每日彩种与大乐透（周一开奖）回到 10-05；双色球仍 09-29。"""
    now = datetime(2026, 10, 5, 21, 30)
    for lot in LOTS_DAILY:
        assert expected_latest_draw_date(lot, now) == date(2026, 10, 5), lot
    assert expected_latest_draw_date("大乐透", now) == date(2026, 10, 5)
    assert expected_latest_draw_date("双色球", now) == date(2026, 9, 29)  # 10-05 周一非双色球日


def test_expected_2027_spring_festival_projection():
    """2027 春节推算窗口（正月初一 02-06 前 3 后 7 → 02-03~02-13）内自动回落。"""
    days = [d for d in range(1, 16) if is_holiday(date(2027, 2, d))]
    assert days == list(range(3, 14))
    now = datetime(2027, 2, 5, 10, 0)
    for lot in LOTS_DAILY:
        assert expected_latest_draw_date(lot, now) == date(2027, 2, 2), lot
    assert expected_latest_draw_date("双色球", now) == date(2027, 2, 2)   # 02-02 周二
    assert expected_latest_draw_date("大乐透", now) == date(2027, 2, 1)   # 02-01 周一
    assert next_draw_date("双色球", date(2027, 2, 5)) == date(2027, 2, 14)


# ---------------------------------------------------------------- web/utils P0
def test_compute_next_draw_date_new_signature():
    """web/utils._compute_next_draw_date：第二参已是彩种名，走休市日历。"""
    from web.utils import _compute_next_draw_date
    assert _compute_next_draw_date("2026-09-29", "双色球") == "2026-10-06"
    assert _compute_next_draw_date("2026-09-30", "大乐透") == "2026-10-05"
    assert _compute_next_draw_date("", "双色球") == ""
    assert _compute_next_draw_date("不是日期", "双色球") == ""
    assert _compute_next_draw_date("2026-09-29", "不存在的彩种") == ""


def test_get_data_status_lag_zero_during_closure():
    """真数据（只读）+ 注入休市日时刻：lag 必须 0、next 与日历真源一致。

    断言动态对齐日历真源，避免未来真实数据更新后用例失效。
    """
    from datetime import timedelta
    from web.utils import get_data_status
    now = datetime(2026, 10, 2, 10, 0)
    for lot in ("双色球", "大乐透", "排列3", "七星彩"):
        s = get_data_status(lot, now=now)
        assert s.get("data_lag_days") == 0, (lot, s)
        latest = datetime.strptime(s["latest_date"][:10], "%Y-%m-%d").date()
        expect = next_draw_date(lot, latest + timedelta(days=1))
        assert s["next_draw_date"] == expect.strftime("%Y-%m-%d"), (lot, s)


# ---------------------------------------------------------------- ev/sales_model P2
def test_sales_model_next_date_skips_closure():
    """sales_model._next_date 走休市日历（休市边缘 gap 变长是真实语义）。"""
    from ev.sales_model import _next_date
    assert _next_date("大乐透", "2026-09-30") == __import__("pandas").Timestamp("2026-10-05")
    assert _next_date("双色球", "2026-09-29") == __import__("pandas").Timestamp("2026-10-06")


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
