# -*- coding: utf-8 -*-
"""ev/stake.py 注数决策测试（策略升级 C）。

契约：
  ① EV≤0 → 全链 0 注 +「纪律不下注」（Kelly 数学结论）；
  ② EV>0 → 实际注数 ≤ 预算注数 ≤ bankroll/cost（三重 min）；
  ③ cap_tickets 外部上限生效；
  ④ 止损/熔断状态 → 资金纪律优先于信号。
"""
import pytest

from ev.stake import stake_plan


def test_negative_ev_means_zero():
    """① EV≤0 → 0 注（本项目常态：所有彩种名义 EV 为负）。"""
    out = stake_plan(ev_per_ticket=-0.96, prob=1 / 1000, bankroll=10000.0)
    assert out["实际建议注数"] == 0
    assert out["理论注数"] == 0
    assert out["kelly比例"] == 0.0
    assert any("纪律不下注" in c for c in out["触发约束"])


def test_positive_ev_respects_budget():
    """② EV>0 且赔率满足 p(b+1)>1 → 注数 ≤ 预算注数 ≤ bankroll/cost。"""
    # ev=3, p=0.5 → b=1.5, f=(0.75-0.5)/1.5≈0.167 → 理论注数超预算 → 预算约束生效
    out = stake_plan(ev_per_ticket=3.0, prob=0.5, bankroll=10000.0)
    assert out["实际建议注数"] > 0
    assert out["实际建议注数"] <= out["预算注数"]
    assert out["预算注数"] * 2.0 <= 10000.0 * 0.02 + 2   # 单期预算 2%
    assert out["kelly比例"] > 0
    assert any("预算" in c for c in out["触发约束"])


def test_zero_ev_means_zero():
    out = stake_plan(ev_per_ticket=0.0, prob=0.5, bankroll=10000.0)
    assert out["实际建议注数"] == 0


def test_cap_tickets_binding():
    """③ 外部单期上限生效（取三者最小）。"""
    # ev=20, p=0.1 → b=10, f=(1.0-0.9)/10=0.01 → 理论 2500 注 > cap 3
    out = stake_plan(ev_per_ticket=20.0, prob=0.1, bankroll=1_000_000.0,
                     cap_tickets=3)
    assert out["实际建议注数"] == 3
    assert any("上限" in c for c in out["触发约束"])


def test_stop_loss_overrides_signal():
    """④ 熔断/止损状态 → 资金纪律优先（信号再好也不下注）。"""
    out = stake_plan(ev_per_ticket=1.0, prob=0.1, bankroll=10000.0,
                     consec_loss=5)
    assert out["实际建议注数"] == 0
    assert out["资金状态"] != "正常"
    assert any("资金纪律" in c for c in out["触发约束"])


def test_quarter_kelly_smaller_than_half():
    """重尾收益 → 1/4 Kelly 注数 ≤ 半 Kelly（诚实边界的量化体现）。"""
    a = stake_plan(ev_per_ticket=3.0, prob=0.5, bankroll=20000.0, frac=0.5)
    b = stake_plan(ev_per_ticket=3.0, prob=0.5, bankroll=20000.0, frac=0.25)
    assert b["kelly比例"] == pytest.approx(a["kelly比例"] / 2, rel=1e-3)
    assert b["实际建议注数"] <= a["实际建议注数"]
