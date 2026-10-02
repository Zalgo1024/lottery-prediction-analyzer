"""启动恢复休市感知（2026-10-02）：休市日永不触发补抓；恢复日按开奖日历判定。"""

from datetime import datetime

import pytest

from web.startup_recovery import _needs_recovery


def _status(lag, next_draw):
    return {"data_lag_days": lag, "next_draw_date": next_draw}


def test_closure_day_never_recovers():
    """休市日（10-02）：即使旁路算出 lag=2 也必须短路——休市日没有新开奖可补。"""
    now = datetime(2026, 10, 2, 10, 0)
    assert _needs_recovery("排列3", _status(2, "2026-10-05"), now=now) is False


def test_closure_day_lag_zero():
    now = datetime(2026, 10, 2, 10, 0)
    assert _needs_recovery("排列3", _status(0, "2026-10-05"), now=now) is False


def test_resume_day_daily_after_cutoff():
    """恢复日（10-05）21:30 后：每日彩种 lag=1 → 补。"""
    now = datetime(2026, 10, 5, 21, 30)
    assert _needs_recovery("排列3", _status(1, "2026-10-06"), now=now) is True


def test_resume_day_weekly_after_cutoff():
    """恢复日 21:30 后：双色球在 10-06（周二，合法开奖日）→ 补。"""
    now = datetime(2026, 10, 6, 21, 30)
    assert _needs_recovery("双色球", _status(1, "2026-10-08"), now=now) is True


def test_non_draw_weekday_no_recovery():
    """非开奖日（2026-09-30 周三，双色球不开）+ 未过恢复条件 → 不补。"""
    now = datetime(2026, 9, 30, 21, 30)
    assert _needs_recovery("双色球", _status(1, "2026-10-01"), now=now) is False


def test_next_draw_passed_recovers_even_outside_window():
    """下期开奖日已过（日历口径已含休市）→ 无论几点都该补。"""
    now = datetime(2026, 10, 5, 9, 0)
    assert _needs_recovery("双色球", _status(2, "2026-10-03"), now=now) is True


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
