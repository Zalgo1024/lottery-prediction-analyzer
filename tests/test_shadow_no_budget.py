# -*- coding: utf-8 -*-
"""影子对照不得占用真实预算（2026-10-31 任务 2 的牙齿）。

用户要求的是「先用**影子模式**跑，不追加投注」。所以最关键的一条不是
「三臂算得对不对」，而是：**跑完影子，真实额度一分没动**。

如果影子会记账，整件事就变质了 —— 它会从「对照实验」变成「偷偷加注」，
而且记账是跨月累计的，一旦脏了，当月后面的额度判断全部偏松。
"""

from __future__ import annotations

import pytest

import ev.budget_gate as BG
from data import shadow as SH


def _tickets(n: int):
    return [{"第1位": [i % 10]} for i in range(n)]


def test_shadow_does_not_touch_budget_ledger():
    """★★ 跑完三臂，`本月已投` 必须与调用前**严格相等**。"""
    before = BG.spent_this_month()
    for arm in SH.ARMS:
        SH.record("排列5", "999001", arm, tickets=_tickets(50),
                  result={"中奖注数": 2, "税前奖金": 20.0})
    assert BG.spent_this_month() == before


def test_recorded_rows_are_marked_unbooked():
    """每行都要自带 `记账=False` —— 这是汇总时能自证的唯一凭据。"""
    SH.record("排列5", "999002", SH.ARM_MODEL, tickets=_tickets(10))
    rows = SH.load("排列5")
    assert rows and all(r["记账"] is False for r in rows)
    SH.assert_no_booking("排列5")


def test_cost_is_computed_but_not_paid():
    """成本照算（对照要用），但只是账面上的数字，不是真花掉的钱。"""
    row = SH.record("排列5", "999003", SH.ARM_RANDOM, tickets=_tickets(25))
    assert row["成本"] == 50.0          # 25 注 × 2 元
    assert row["净盈亏"] == -50.0


def test_unknown_arm_is_rejected():
    with pytest.raises(ValueError):
        SH.record("排列5", "999004", "随便加的臂", tickets=_tickets(1))


def test_same_budget_flags_different_ticket_counts():
    """★ 注数不一致必须点名，否则「买得更多」会被误读成「选得更好」。"""
    SH.record("排列5", "1", SH.ARM_MODEL, tickets=_tickets(10))
    SH.record("排列5", "1", SH.ARM_RANDOM, tickets=_tickets(50))
    s = SH.summary("排列5")
    assert s["同预算"]["同注数"] is False
    assert s["同预算"]["可比较"] is False
    assert "买了更多" in s["同预算"]["说明"]


def test_same_budget_passes_when_aligned():
    for arm in SH.ARMS:
        SH.record("排列5", "1", arm, tickets=_tickets(20))
        SH.record("排列5", "2", arm, tickets=_tickets(20))
    s = SH.summary("排列5")
    assert s["同预算"]["可比较"] is True
    assert s["同预算"]["同窗口"] is True
    assert s["同预算"]["共同期数"] == 2


def test_same_budget_flags_different_windows():
    SH.record("排列5", "1", SH.ARM_MODEL, tickets=_tickets(5))
    SH.record("排列5", "2", SH.ARM_RANDOM, tickets=_tickets(5))
    s = SH.summary("排列5")
    assert s["同预算"]["同窗口"] is False
    assert "跑了不同的期" in s["同预算"]["说明"]


def test_failed_period_still_gets_a_row():
    """失败期必须留行 —— 只留成功期等于把不利样本删掉。"""
    SH.record("排列5", "100", SH.ARM_MODEL, tickets=[],
              result={"结算失败": True}, note="数据缺失")
    rows = SH.load("排列5")
    assert len(rows) == 1 and rows[0]["结算失败"] is True
    s = SH.summary("排列5")
    assert s["臂"][SH.ARM_MODEL]["结算失败期数"] == 1
    assert s["臂"][SH.ARM_MODEL]["期数"] == 1


def test_honest_note_is_present():
    s = SH.summary("排列5")
    assert "不因选号方式改变" in s["诚实边界"]
