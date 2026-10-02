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


def test_holiday_priority_override_first(monkeypatch):
    """休市判定优先级：手工覆盖 > 精确休市表 > 未来推算。

    2026-09-21 重写：此前用 chinese_calendar 的「法定节假日」当休市口径，
    与事实不符（元旦/清明/五一/端午/中秋实际照常开奖，实测 85 期开奖日被误判）。
    现在只认 config.MARKET_CLOSURE（真实数据反推）+ 未来年份推算。
    """
    import data.holiday as h

    # ① 手工覆盖优先：把 2026-09-20 手工设为休市，则 override 生效
    monkeypatch.setitem(h.HOLIDAY_OVERRIDE, 2026, [(9, 20, 1)])
    assert h.is_holiday(date(2026, 9, 20)) is True
    monkeypatch.setitem(h.HOLIDAY_OVERRIDE, 2026, [])

    # ② 精确表：春节/国庆休市
    assert h.is_holiday(date(2026, 2, 17)) is True    # 正月初一
    assert h.is_holiday(date(2026, 10, 1)) is True    # 国庆
    # ③ 精确表：表内非窗口日一律开市（含调休上班的周末）
    assert h.is_holiday(date(2026, 9, 20)) is False
    assert h.is_holiday(date(2026, 9, 6)) is False


def test_legal_holidays_are_normal_draw_days():
    """元旦/清明/五一/端午/中秋照常开奖——实测 397 天铁证。

    这些日子在 chinese_calendar 里是「法定节假日」，但彩票市场并不休市
    （例：本地 CSV 里 2026-01-01、2026-04-04、2025-05-01 都有开奖记录）。
    """
    for d in (date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
              date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
              date(2025, 5, 1), date(2025, 5, 5), date(2025, 10, 6)):
        assert is_holiday(d) is False, f"{d} 是法定节假日但市场照常开奖"
        assert is_draw_day("福彩3D", d) is True, f"{d} 福彩3D（天天开）应当是开奖日"


def test_spring_festival_and_national_day_are_closed():
    """春节/国庆休市（表内精确窗口）。"""
    # 2026 春节 02-14 ~ 02-23
    for d in (date(2026, 2, 14), date(2026, 2, 17), date(2026, 2, 23)):
        assert is_holiday(d) is True
        assert is_draw_day("福彩3D", d) is False
    assert is_holiday(date(2026, 2, 13)) is False   # 窗口前一天开市
    assert is_holiday(date(2026, 2, 24)) is False   # 窗口后一天开市
    # 2025 国庆 10-01 ~ 10-04
    for d in (date(2025, 10, 1), date(2025, 10, 4)):
        assert is_holiday(d) is True
    assert is_holiday(date(2025, 10, 5)) is False
    # 2005-2018 国庆不休市（照常开奖）
    assert is_holiday(date(2015, 10, 1)) is False
    assert is_holiday(date(2018, 10, 3)) is False


def test_future_year_projection():
    """表外年份（2027+）的推算兜底：春节窗口 = 正月初一前 3 天 ~ 后 7 天；国庆 10-01~04。"""
    # 2027 春节 = 2027-02-06 → 窗口 02-03 ~ 02-13
    assert is_holiday(date(2027, 2, 3)) is True
    assert is_holiday(date(2027, 2, 6)) is True
    assert is_holiday(date(2027, 2, 13)) is True
    assert is_holiday(date(2027, 2, 2)) is False
    assert is_holiday(date(2027, 2, 14)) is False
    # 2028 春节 = 2028-01-26 → 窗口 01-23 ~ 02-02
    assert is_holiday(date(2028, 1, 26)) is True
    # 国庆（2020 年起固定 10-01 ~ 10-04）
    assert is_holiday(date(2027, 10, 2)) is True
    assert is_holiday(date(2027, 10, 5)) is False
    # 表外年份的元旦不休市
    assert is_holiday(date(2027, 1, 1)) is False


def test_makeup_workday_is_draw_day():
    """2026-09-20（周日·国庆调休上班）必须是开奖日。

    实锤：本地 CSV 里双色球 26109 / 福彩3D 2026253 / 排列5 26253 当天都开奖了，
    但旧逻辑把它判成休市 → predict_target 的「预测日期」被推到 09-21/09-22 →
    结算时 _is_valid_prediction(预测日期 > 开奖日期) 把开奖前出好的真预测
    误标成「开奖后回测·训练」（09-21 核查 646 条训练标签、0 条确认是开奖后生成）。
    """
    assert is_holiday(date(2026, 9, 20)) is False
    assert is_draw_day("福彩3D", date(2026, 9, 20)) is True
    assert is_draw_day("排列5", date(2026, 9, 20)) is True
    assert is_draw_day("双色球", date(2026, 9, 20)) is True
    # 双色球周日开奖：从 09-18 起算的下一个开奖日必须是 09-20，而不是 09-22
    assert next_draw_date("双色球", date(2026, 9, 18)) == date(2026, 9, 20)


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


def test_valid_prediction_prefers_generation_time():
    """真伪判定优先看生成时间（事实），不再单靠派生字段「预测日期」。

    2026-09-21：`预测日期` 由开奖日历推算，日历一偏就被推到下一期，
    导致开奖前出好的真预测被判成「开奖后回测·训练」。生成时间不受其影响。
    """
    from data.feedback import _is_valid_prediction

    draw_day = date(2026, 9, 20)
    # 开奖前 3 天生成 → 真预测（旧口径：预测日期 09-22 > 开奖日 09-20 → 误判训练）
    assert _is_valid_prediction("2026-09-22", draw_day, "2026-09-17T21:31:21") is True
    # 开奖日 00:47 生成 → 真预测
    assert _is_valid_prediction("2026-09-21", draw_day, "2026-09-20T00:47:38") is True
    # 开奖日 22:05 生成（当晚出号）→ 马后炮
    assert _is_valid_prediction("2026-09-20", draw_day, "2026-09-20T22:05:00") is False
    # 无生成时间 → 回退日期规则（保守沿用原口径）
    assert _is_valid_prediction("2026-09-22", draw_day) is False
    assert _is_valid_prediction("2026-09-20", draw_day) is True


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
