# -*- coding: utf-8 -*-
"""ev/threshold.py 门槛计算器测试（策略升级 E）。

契约：
  ① 解析门槛 = θ·cap·μ/pool_share（精确值）；
  ② 精确门槛 ≥ 解析门槛（泊松散布使「期望实得顶格」比逐点式更严）；
  ③ μ 增大 ⇒ 门槛单调不减；
  ④ feasibility_report 判定 ∈ 三档白名单，且必含诚实边界；
  ⑤ 达门槛时的边际核算走「分元级」判定（真实量级）。
"""
import pytest

import ev.threshold as th
from ev.payout import pool_share_expected

CAP = 10_000_000


def test_analytic_threshold_exact():
    """① 解析门槛 = θ·cap·μ/pool_share。"""
    mu, theta, ps = 100.0, 0.999, 0.7
    out = th.pool_threshold(mu, theta=theta, pool_share=ps)
    assert out["解析门槛_池"] == pytest.approx(theta * CAP * mu / ps, rel=1e-9)
    assert out["μ"] == mu


def test_precise_ge_analytic():
    """② 精确门槛 ≥ 解析门槛（恒成立，泊松散布）。"""
    for mu in (1.0, 10.0, 225.0, 5000.0):
        out = th.pool_threshold(mu)
        assert out["精确门槛_池"] >= out["解析门槛_池"]


def test_threshold_monotone_in_mu():
    """③ μ 增大 ⇒ 门槛单调不减。"""
    a = th.pool_threshold(50.0)
    b = th.pool_threshold(500.0)
    c = th.pool_threshold(5000.0)
    assert a["解析门槛_池"] < b["解析门槛_池"] < c["解析门槛_池"]
    assert a["精确门槛_池"] <= b["精确门槛_池"] <= c["精确门槛_池"]


def test_precise_threshold_matches_root():
    """精确门槛处目标函数确实达标（求根正确性）。"""
    mu = 100.0
    out = th.pool_threshold(mu)
    hp = out["精确门槛_池"] * 0.7          # 池可分池
    eff = pool_share_expected(hp, mu)
    assert eff >= 0.999 * CAP


def test_mu_nonpositive_honest():
    out = th.pool_threshold(0.0)
    assert out["解析门槛_池"] is None
    assert "不可估" in out["口径说明"]


def test_feasibility_below_threshold(monkeypatch):
    """④ 池低于门槛 → 「池未达门槛」+ 诚实边界必在。"""
    monkeypatch.setattr("ev.rollover._load_history",
                        lambda name: _mini_df(sales=8e9, pool=1e6))
    out = th.feasibility_report(lottery="双色球", pool=1e6)
    assert out["实践可行性判定"] == "池未达门槛"
    assert "不因门槛达成而改变" in out["诚实边界"]
    assert set(out["两档门槛"]) == {"冷门(μ_cold)", "随机(μ_rand=λ)"}


def test_feasibility_marginal_is_cents(monkeypatch):
    """⑤ 池达门槛 → 真实量级下边际必然是分元级（注数×p1×边际）。"""
    monkeypatch.setattr("ev.rollover._load_history",
                        lambda name: _mini_df(sales=8e9, pool=8e9))
    out = th.feasibility_report(lottery="双色球", pool=8e9)
    assert out["实践可行性判定"].startswith("达门槛")
    if "边际核算" in out:
        assert out["边际核算"]["单期边际合计"] < 0.01


def _mini_df(sales: float, pool: float):
    import pandas as pd
    return pd.DataFrame({"期号": ["26111"], "奖池奖金": [pool], "总投注额": [sales]})
