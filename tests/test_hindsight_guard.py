"""
马后炮防御（data/feedback.py + data/holiday.py）测试

背景：2026-09-06 双色球 26103 期，数据滞后时流水线仍对已开奖期出号，
50 条马后炮积压到深夜一次性结算。修复 = ①开奖日历只认法定节假日（普通
周末照常开奖）②pending 入库前拦截"目标期已开奖"的出号 ③目标期号与预测
日期同源推算（predict_target）。
"""

from datetime import date, datetime

import pytest

import data.feedback as fb
from data.feedback import (
    load_pending,
    predict_target,
    record_pending_prediction,
)
from data.holiday import is_draw_day, is_holiday, next_draw_date


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "_pending_path", lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path", lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path", lambda name: tmp_path / f"{name}_weights.json")
    return tmp_path


# ------------------------------------------------------------
# 开奖日历：普通周末照常开奖，法定节假日休市
# ------------------------------------------------------------

def test_sunday_is_ssq_draw_day():
    """2026-09-06 周日是双色球开奖日（历史 bug：chinese_calendar 把周末当假日）。"""
    assert is_holiday(date(2026, 9, 6)) is False
    assert is_draw_day("双色球", date(2026, 9, 6)) is True
    assert next_draw_date("双色球", date(2026, 9, 6)) == date(2026, 9, 6)


def test_saturday_is_dlt_draw_day():
    assert is_holiday(date(2026, 9, 12)) is False
    assert is_draw_day("大乐透", date(2026, 9, 12)) is True


def test_national_day_is_holiday():
    assert is_holiday(date(2026, 10, 1)) is True
    assert is_draw_day("双色球", date(2026, 10, 1)) is False


# ------------------------------------------------------------
# 马后炮拦截
# ------------------------------------------------------------

def test_predict_target_consistent_pair():
    """predict_target：期号 = 本地最大期号+1，开奖日非空且晚于本地最近开奖日。"""
    from data.loader import load_lottery

    data = load_lottery("排列5")
    max_issue = max(r.期号 for r in data.records if r.期号)
    last_date = max(r.开奖日期 for r in data.records if r.开奖日期)
    issue, draw_day = predict_target("排列5")
    assert issue == max_issue + 1
    assert draw_day is not None and draw_day > last_date


def test_guard_rejects_issue_already_in_data(tmp_fb, monkeypatch):
    """目标期号已存在于本地 CSV → 该期已开奖，拒绝入库。"""
    from data.loader import load_lottery

    max_issue = max(r.期号 for r in load_lottery("排列5").records if r.期号)
    rec = {
        "预测日期": "2099-01-01",
        "预测号码": [],
        "号码组数": 0,
        "目标期号": max_issue,
        "生成时间": datetime.now().isoformat(),
    }
    res = record_pending_prediction("排列5", rec)
    assert res["状态"].startswith("已拒绝")
    assert load_pending("排列5") == []


def test_guard_rejects_draw_time_passed(tmp_fb, monkeypatch):
    """推算开奖日就是今天、但生成时间已过 20:00 → 拒绝（当晚出号=马后炮）。"""
    monkeypatch.setattr(
        fb, "_target_draw_date", lambda lot, issue: date.today())
    rec = {
        "预测日期": str(date.today()),
        "预测号码": [],
        "号码组数": 0,
        "目标期号": 999001,
        "生成时间": datetime.now().replace(hour=22, minute=5).isoformat(),
    }
    res = record_pending_prediction("排列5", rec)
    assert res["状态"] == "已拒绝(开奖时间已过)"
    assert load_pending("排列5") == []


def test_guard_allows_morning_prediction_for_tonight(tmp_fb, monkeypatch):
    """推算开奖日就是今天、生成时间在白天 → 放行（开奖前的正常预测）。"""
    monkeypatch.setattr(
        fb, "_target_draw_date", lambda lot, issue: date.today())
    rec = {
        "预测日期": str(date.today()),
        "预测号码": [],
        "号码组数": 0,
        "目标期号": 999001,
        "生成时间": datetime.now().replace(hour=10, minute=0).isoformat(),
    }
    record_pending_prediction("排列5", rec)
    pending = load_pending("排列5")
    assert len(pending) == 1 and pending[0]["目标期号"] == 999001


def test_guard_allows_future_draw(tmp_fb):
    """推算开奖日在未来（正常场景）→ 放行。"""
    rec = {
        "预测日期": "2099-01-01",
        "预测号码": [],
        "号码组数": 0,
        "目标期号": 999002,  # 远超 _target_draw_date 400 天搜索窗 → None → 保守放行
    }
    record_pending_prediction("排列5", rec)
    assert len(load_pending("排列5")) == 1
