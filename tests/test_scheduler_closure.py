"""调度器休市感知（2026-10-02）：休市窗口内不排班、不空跑流水线。

约定：建 AutoScheduler 后立刻 `scheduler._save = lambda: None`，
防止测试写 config/auto_scheduler_state.json（沿用 test_scheduler.py 惯例）。
"""

from datetime import datetime

import pytest

from web.scheduler import AutoScheduler


def _make():
    s = AutoScheduler()
    s._save = lambda: None
    return s


def test_next_run_none_on_closure_day_for_daily():
    """每日彩种：休市日（10-02）之前 15 天窗口内最近开奖日是 09-30（仍会返回），
    但休市日当天 22:05 不是触发点——语义是「最近应已触发」，此处锁回落行为。"""
    s = _make()
    due = s._next_run("排列3", now=datetime(2026, 10, 2, 23, 30))
    assert due == datetime(2026, 9, 30, 22, 5)   # 回落到休市前最后开奖日


def test_next_future_run_skips_closure():
    """展示口径：休市期「下次运行」必须落在恢复后首个开奖日 22:05。"""
    s = _make()
    now = datetime(2026, 10, 2, 10, 0)
    assert s._next_future_run("排列3", now=now) == datetime(2026, 10, 5, 22, 5)
    assert s._next_future_run("双色球", now=now) == datetime(2026, 10, 6, 22, 5)
    assert s._next_future_run("大乐透", now=now) == datetime(2026, 10, 5, 22, 5)
    assert s._next_future_run("七星彩", now=now) == datetime(2026, 10, 6, 22, 5)


def test_no_overdue_when_closure_run_was_done():
    """休市日当天：09-30 那班已跑过 → overdue=False（休市不产生新的应触发点）。"""
    s = _make()
    now = datetime(2026, 10, 2, 23, 30)
    for lot in ("排列3", "双色球"):
        s.state[lot] = {"last_run": "2026-09-30T22:06:00", "last_status": "done"}
    rows = {x["lottery"]: x for x in s.get_status(now=now)["lotteries"]}
    assert rows["排列3"]["overdue"] is False
    assert rows["双色球"]["overdue"] is False


def test_overdue_on_resume_day_evening():
    """恢复日晚间：若休市前最后开奖日的班没跑，overdue 必须能报出来。"""
    s = _make()
    now = datetime(2026, 10, 5, 23, 30)
    s.state["排列3"] = {"last_run": "2026-09-29T22:06:00", "last_status": "done"}
    rows = {x["lottery"]: x for x in s.get_status(now=now)["lotteries"]}
    assert rows["排列3"]["overdue"] is True


def test_long_closure_returns_none():
    """2020 疫情级超长休市（49 天）：15 天有界窗口内找不到开奖日 → None = 不触发，无死循环。"""
    s = _make()
    # 2020-01-22 ~ 2020-03-10 全市场停售
    assert s._next_run("排列3", now=datetime(2020, 2, 15, 23, 30)) is None
    assert s._next_future_run("排列3", now=datetime(2020, 2, 15, 10, 0)) is None


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
