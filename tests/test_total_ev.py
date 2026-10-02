# -*- coding: utf-8 -*-
"""ev/total_ev.py 三口径对照表测试（策略升级 D）。

全部用 monkeypatch 伪造流行度模型与历史数据（纯函数断言，不触发重回测/重拟合）。
关键契约：
  ① 固定奖级三口径逐行相等（期望线性性）；
  ② 冷门口径 ≥ 热门口径（μ 单调），且 ≤ min(cap, head_pool)；
  ③ 池巨大时两口径封顶饱和（≈cap）；
  ④ 大乐透 NO-Go → λ 回退 + 显式 note，不抛异常；
  ⑤ λ均摊列与 ev/rollover.rollover_ev 的总EV 精确对齐（同源锚点）。
"""
import numpy as np
import pandas as pd
import pytest

import ev.popularity as pop
import ev.rollover as ro
import ev.total_ev as te
from ev.popularity import features_matrix

BETA = np.array([0.5, -0.3, 0.2, 0.1, -0.2, 0.05, 0.15, 0.1, 0.2])
M_ = np.zeros(9)
S_ = np.ones(9)
POOL = 2_000_000_000.0
SALES = 350_000_000.0
CAP = 10_000_000


def _df():
    return pd.DataFrame({"期号": ["26110"], "奖池奖金": [POOL], "总投注额": [SALES]})


@pytest.fixture
def fake_env(monkeypatch):
    """GO 假模型 + 冷/热组合按真实特征分排序（保证 μ_cold < μ_hot）。"""
    monkeypatch.setattr(ro, "_load_history", lambda name: _df())
    A = [1, 2, 3, 4, 5, 6]
    B = [1, 9, 17, 23, 29, 33]
    F = features_matrix(np.array([A, B], dtype=np.int16))
    z = ((F - M_) / S_) @ BETA
    cold, hot = (A, B) if z[0] <= z[1] else (B, A)
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": BETA, "m_": M_, "s_": S_,
                                     "report": {}})
    monkeypatch.setattr(pop, "cold_hot_combo",
                        lambda lot, q_cold=0.1, q_hot=0.9: (list(cold), list(hot)))
    return {"cold": cold, "hot": hot}


def test_fixed_grades_identical_across_three_bases(fake_env):
    """① 固定奖级三口径逐行相等（期望线性性：选号不改固定奖）。"""
    out = te.prize_split_table("双色球")
    assert out["applicable"] is True
    assert out["分摊口径"] == "μ_冷门/μ_热门(流行度GO)"
    fixed = [r for r in out["逐奖级表"] if not r["浮动"]]
    assert fixed, "双色球应有固定奖级行"
    for r in fixed:
        assert r["λ均摊单注"] == r["冷门口径单注"] == r["热门口径单注"] == r["名义单注"]
        assert r["贡献_λ"] == pytest.approx(r["贡献_冷门"])
        assert r["贡献_冷门"] == pytest.approx(r["贡献_热门"])
        assert "期望线性性" in r["备注"]


def test_cold_ge_hot_and_bounded(fake_env):
    """② 冷门口径 ≥ 热门口径（μ_cold<μ_hot 单调），且 ≤ min(cap, head_pool)。"""
    out = te.prize_split_table("双色球")
    row1 = out["逐奖级表"][0]
    assert row1["奖级"] == "一等"
    assert row1["冷门口径单注"] >= row1["热门口径单注"]
    head_pool = POOL * 0.7
    assert row1["冷门口径单注"] <= min(CAP, head_pool) + 1e-6
    evs = out["EV汇总"]
    assert evs["ΔEV_冷门_vs_热门"] >= 0
    assert evs["冷门口径"] >= evs["热门口径"]


def test_cap_saturation(fake_env):
    """③ 池巨大 → 泊松散布内全部被 cap 截断，冷/热口径都 ≈ cap（冷门优势被压缩）。"""
    out = te.prize_split_table("双色球", pool=1e15, n_tickets=SALES / 2.0)
    row1 = out["逐奖级表"][0]
    assert row1["冷门口径单注"] == pytest.approx(CAP, abs=0.01)
    assert row1["热门口径单注"] == pytest.approx(CAP, abs=0.01)
    evs = out["EV汇总"]
    assert evs["ΔEV_冷门_vs_热门"] == pytest.approx(0.0, abs=1e-4)


def test_dlt_nogo_degrades(monkeypatch):
    """④ 大乐透（WP1 NO-Go 现状）→ λ 回退 + 显式 note，冷/热列为 None，不抛异常。"""
    monkeypatch.setattr(ro, "_load_history", lambda name: _df())
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": False, "report": {}})
    out = te.prize_split_table("大乐透")
    assert out["applicable"] is True
    assert out["分摊口径"] == "λ均摊(回退)"
    assert "Go" in out["note"]
    row1 = out["逐奖级表"][0]
    assert row1["冷门口径单注"] is None and row1["热门口径单注"] is None
    evs = out["EV汇总"]
    assert evs["冷门口径"] is None and evs["ΔEV_冷门_vs_λ"] is None
    assert evs["λ均摊"] == pytest.approx(
        -2.0 + row1["贡献_λ"] + sum(r["贡献_λ"] for r in out["逐奖级表"][1:]))


def test_lambda_column_matches_rollover(fake_env):
    """⑤ λ均摊列与 ev/rollover.rollover_ev 的总EV 精确对齐（同源锚点）。"""
    out = te.prize_split_table("双色球")
    r = ro.rollover_ev("双色球")
    assert out["EV汇总"]["λ均摊"] == pytest.approx(r["总EV(每注)"], abs=1e-6)
    # 同一数据源：期号/奖池一致
    assert out["期号"] == r["期号"]
    assert out["奖池"] == r["奖池"]


def test_inapplicable_lotteries():
    """七星彩/数字型：机制不适用，显式 note 指路。"""
    for lot in ("七星彩", "排列5", "福彩3D", "排列3"):
        out = te.prize_split_table(lot)
        assert out["applicable"] is False
        assert out.get("note")
        assert out["诚实边界"]


def test_total_ev_compare_fills_backtest(monkeypatch, fake_env):
    """对外入口：回填反事实回测旁证 + 分档结论必在。"""
    monkeypatch.setattr(pop, "counterfactual_backtest",
                        lambda lot, q_cold=0.1, q_hot=0.9: {"go": True,
                                                            "实得提升倍数_中位": 1.484})
    out = te.total_ev_compare("双色球")
    assert out["历史提升倍数参考"] == pytest.approx(1.484)
    assert out.get("分档结论")
