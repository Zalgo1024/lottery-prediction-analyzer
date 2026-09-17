"""
奖金表回归测试（data/prize_table.py）。

覆盖：
1. 六彩种固定奖级金额与官方规则一致
2. 命名兼容 —— 乐透型 "一等" 与 七星彩 "一等奖" 都能查到
3. 浮动奖级（双/大/七星彩一、二等）：有当期实际数据用实际值，无则给名义参考并标浮动
4. 未中 / 未知奖级 → 0 元且来源为「未中」
5. 净盈亏（扣 2 元成本）

纯逻辑测试：浮动分支通过 monkeypatch 注入构造数据，不依赖真实 CSV。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import data.prize_table as pt
from data.prize_table import prize_payout, gross_and_net


@pytest.fixture(autouse=True)
def _clean_cache():
    pt.clear_cache()
    yield
    pt.clear_cache()


# ---------- 1. 固定奖级 ----------

@pytest.mark.parametrize("grade,expect", [
    ("三等", 3000), ("四等", 200), ("五等", 10), ("六等", 5),
])
def test_ssq_fixed(grade, expect):
    r = prize_payout("双色球", grade)
    assert r["金额"] == expect
    assert r["浮动"] is False
    assert r["来源"] == "固定奖金"


@pytest.mark.parametrize("grade,expect", [
    ("三等", 10000), ("四等", 3000), ("五等", 300), ("六等", 200),
    ("七等", 100), ("八等", 15), ("九等", 5),
])
def test_dlt_fixed(grade, expect):
    assert prize_payout("大乐透", grade)["金额"] == expect


@pytest.mark.parametrize("lottery", ["福彩3D", "排列3"])
def test_digit3_fixed(lottery):
    assert prize_payout(lottery, "直选")["金额"] == 1040
    assert prize_payout(lottery, "组选3")["金额"] == 346
    assert prize_payout(lottery, "组选6")["金额"] == 173


def test_pl5_and_qxc_fixed():
    assert prize_payout("排列5", "直选")["金额"] == 100000
    assert prize_payout("七星彩", "三等")["金额"] == 3000
    assert prize_payout("七星彩", "四等")["金额"] == 500
    assert prize_payout("七星彩", "五等")["金额"] == 30
    assert prize_payout("七星彩", "六等")["金额"] == 5


def test_all_six_lotteries_registered():
    """6 彩种都在表内（防止新增彩种漏登记 → 静默显示「—」）。"""
    for name in ("双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"):
        assert name in pt.FIXED_PAYOUT or name in pt.FLOATING_REF, f"{name} 未登记奖金表"


# ---------- 2. 命名兼容 ----------

@pytest.mark.parametrize("a,b", [
    ("一等奖", "一等"), ("五等奖", "五等"), ("九等奖", "九等"),
])
def test_grade_name_compat(a, b):
    """乐透型「N等」与七星彩「N等奖」命名互通（feedback 里两套并存）。"""
    assert pt._norm_grade(a) == pt._norm_grade(b)


def test_qxc_style_grade_hits_ssq_table():
    """七星彩写法查双色球奖级：'五等奖' 应等价 '五等' → 10 元。"""
    assert prize_payout("双色球", "五等奖")["金额"] == 10


# ---------- 3. 浮动奖级 ----------

def test_floating_without_actual_is_nominal(monkeypatch):
    """不读真实数据 → 浮动奖级走名义参考分支。"""
    monkeypatch.setattr(pt, "_load_actual_payouts", lambda lot: {})
    r = prize_payout("双色球", "一等", 26105)
    assert r["浮动"] is True
    assert r["金额"] is None
    assert r["来源"] == "名义参考"
    assert "浮动" in r["文本"]


def test_floating_uses_actual_when_available(monkeypatch):
    monkeypatch.setattr(pt, "_load_actual_payouts",
                        lambda lot: {26105: {"一等": 8888888.0, "二等": 123456.0}})
    r = prize_payout("双色球", "一等奖", 26105)
    assert r["金额"] == 8888888.0
    assert r["浮动"] is True
    assert r["来源"] == "当期实际单注奖金"
    assert r["文本"] == "¥8,888,888"

    r2 = prize_payout("双色球", "二等", 26105)
    assert r2["金额"] == 123456.0


def test_floating_missing_issue_falls_back(monkeypatch):
    """期号不在数据里 → 降级为名义参考，不抛异常。"""
    monkeypatch.setattr(pt, "_load_actual_payouts", lambda lot: {26105: {"一等": 1.0}})
    r = prize_payout("大乐透", "一等", 99999)
    assert r["来源"] == "名义参考"
    assert r["金额"] is None


def test_qxc_second_prize_floating_no_ref(monkeypatch):
    """七星彩二等奖浮动 + 阻断实际数据 → 走「视奖池」分支，不应崩。"""
    monkeypatch.setattr(pt, "_load_actual_payouts", lambda lot: {})
    r = prize_payout("七星彩", "二等", 26104)
    assert r["浮动"] is True
    assert "浮动" in r["文本"]


# ---------- 4. 未中 / 未知 ----------

@pytest.mark.parametrize("lottery", ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"])
def test_not_won(lottery):
    r = prize_payout(lottery, "未中")
    assert r["金额"] == 0.0
    assert r["来源"] == "未中"
    assert r["文本"] == "—"


def test_unknown_lottery_or_grade():
    assert prize_payout("不存在的彩种", "五等")["文本"] == "—"
    assert prize_payout("双色球", "特等奖")["文本"] == "—"
    assert prize_payout("双色球", "")["文本"] == "—"


# ---------- 5. 净盈亏 ----------

def test_gross_and_net():
    # 五等奖 10 元，成本 2 元 → 净 +8
    assert gross_and_net("双色球", "五等")["净盈亏"] == 8.0
    # 未中 → 0（命中历史页只看「能中多少」，不计入未购票的成本）
    assert gross_and_net("双色球", "未中")["净盈亏"] == 0.0


def test_gross_and_net_floating_unknown(monkeypatch):
    """浮动奖级无当期实际值 → 净盈亏 None（金额未知，无法计算）。"""
    monkeypatch.setattr(pt, "_load_actual_payouts", lambda lot: {})
    assert gross_and_net("双色球", "一等", 26105)["净盈亏"] is None


def test_amount_formatting():
    assert prize_payout("双色球", "五等")["文本"] == "¥10"
    assert prize_payout("福彩3D", "直选")["文本"] == "¥1,040"
    assert prize_payout("排列5", "直选")["文本"] == "¥100,000"
