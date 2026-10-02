"""休市窗口 closure_window / 看板「休市中」提示条数据源 get_market_closure 回归。"""

from datetime import date

import pytest

from data.holiday import closure_window
from web.utils import get_market_closure


def test_closure_window_national_day():
    w = closure_window(date(2026, 10, 2))
    assert w["start"] == date(2026, 10, 1)
    assert w["end"] == date(2026, 10, 4)
    assert w["days"] == 4
    assert w["name"] == "国庆"
    assert w["resume"] == date(2026, 10, 5)


def test_closure_window_edges():
    """窗口末日仍返回窗口；窗口外前后一天均返回 None。"""
    assert closure_window(date(2026, 10, 4))["end"] == date(2026, 10, 4)
    assert closure_window(date(2026, 10, 5)) is None
    assert closure_window(date(2026, 9, 30)) is None


def test_closure_window_spring_festival_2026():
    w = closure_window(date(2026, 2, 18))
    assert w["name"] == "春节"
    assert w["start"] == date(2026, 2, 14)
    assert w["end"] == date(2026, 2, 23)
    assert w["resume"] == date(2026, 2, 24)


def test_closure_window_projected_2027():
    """表外年份走春节推算兜底：2027-02-03~02-13。"""
    w = closure_window(date(2027, 2, 5))
    assert w is not None
    assert w["name"] == "春节"
    assert w["start"] == date(2027, 2, 3)
    assert w["end"] == date(2027, 2, 13)
    assert w["resume"] == date(2027, 2, 14)


REQUIRED_KEYS = {"closed", "name", "start", "end", "resume_date", "days_left", "today", "message"}


def test_get_market_closure_closed_keys():
    out = get_market_closure(date(2026, 10, 2))
    assert set(out) == REQUIRED_KEYS
    assert out["closed"] is True
    assert out["name"] == "国庆"
    assert out["start"] == "2026-10-01" and out["end"] == "2026-10-04"
    assert out["resume_date"] == "2026-10-05"
    assert out["days_left"] == 3          # 含今天：10-02/03/04
    assert "10-05" in out["message"]
    assert "还剩 3 天" in out["message"]


def test_get_market_closure_open_keys():
    """非休市：键齐全（前端不必判空），closed=False、字段为空。"""
    out = get_market_closure(date(2026, 9, 30))
    assert set(out) == REQUIRED_KEYS
    assert out["closed"] is False
    assert out["days_left"] == 0
    assert out["message"] == ""
    assert out["resume_date"] == ""


def test_message_names_are_dynamic():
    """春节/国庆名称来自窗口月份推断，不许硬编码「国庆」。"""
    a = get_market_closure(date(2026, 10, 2))
    b = get_market_closure(date(2027, 2, 5))
    assert a["name"] == "国庆" and b["name"] == "春节"
    assert a["name"] != b["name"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
