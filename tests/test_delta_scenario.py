# -*- coding: utf-8 -*-
"""tests/test_delta_scenario.py —— δ 情景（v1.8 用户指令二）。

锁三件事：
  ① v1.7 的口径错误被钉住：**二等及以下对 δ 不敏感**，旧口径才是它当年变正的原因；
  ② 封顶不会被「奖池误差」偷偷放掉（缩池，不缩实得）；
  ③ 结构性上限与奖池规模无关 —— 它才是「无论 δ 多大都不可能转正」的依据。
"""
from __future__ import annotations

import pytest

from ev.profit import delta_scenario as DS
from ev.profit.payouts import Ctx
from ev.profit.rules import rules_for


def _ctx(lottery="双色球", issue="26122", *, pool_scale=1.0, total=None):
    r = rules_for(lottery, issue)
    if lottery == "双色球":
        sales = 3.5e8
        pools = {"一等": 1.1e8, "二等": 2.0e7}
    else:
        sales = 2.4e8
        pools = {"一等": 6.0e7, "二等": 1.0e7}
    return Ctx(lottery=lottery, rules=r, pool_before=1.0e9,
               sales_forecast=sales,
               total_tickets=float(total if total is not None else sales / r.avg_ticket_price),
               float_pool={k: v * pool_scale for k, v in pools.items()})


# ---------------------------------------------------------------- ① 口径
def test_delta_zero_equals_baseline_both_conventions():
    ctx = _ctx()
    a = DS.evaluate(ctx, 0.0)
    b = DS.evaluate(ctx, 0.0, legacy=True)
    assert a["净EV_元每注"] == pytest.approx(b["净EV_元每注"], abs=1e-12)
    for g in a["分奖级"]:
        assert a["分奖级"][g]["贡献税后_元每注"] == pytest.approx(
            b["分奖级"][g]["贡献税后_元每注"], abs=1e-12)


def test_second_prize_is_immune_to_delta_in_main_convention():
    """★★ v1.7 的错误就在这一条：主口径下二等奖与「我这注冷门度」无关。"""
    ctx = _ctx()
    d0, d1 = DS.evaluate(ctx, 0.0)["分奖级"]["二等"], DS.evaluate(ctx, 1.0)["分奖级"]["二等"]
    assert d1["mu_others"] == pytest.approx(d0["mu_others"], rel=1e-12)
    assert d1["贡献税后_元每注"] == pytest.approx(d0["贡献税后_元每注"], rel=1e-12)


def test_small_grades_fixed_prizes_ignore_delta():
    ctx = _ctx()
    a, b = DS.evaluate(ctx, 0.0)["分奖级"], DS.evaluate(ctx, 1.0)["分奖级"]
    for g in ("三等", "四等", "五等", "六等"):
        if g in a:
            assert a[g]["kind"] == "fixed"
            assert b[g]["贡献税后_元每注"] == pytest.approx(a[g]["贡献税后_元每注"], rel=1e-12)


def test_legacy_convention_shrinks_second_prize_mu():
    """旧口径（v1.7 的做法）确实把二等奖分母也缩了 —— 那是模型外推。"""
    ctx = _ctx()
    d0 = DS.evaluate(ctx, 0.0, legacy=True)["分奖级"]["二等"]
    d1 = DS.evaluate(ctx, 1.0, legacy=True)["分奖级"]["二等"]
    assert d1["mu_others"] == 0.0
    assert d1["期望实得"] > d0["期望实得"]


def test_legacy_is_more_optimistic_than_main():
    """旧口径系统性高估 ⇒ 它给出的「正情景」不能作实投依据。"""
    ctx = _ctx()
    main = DS.evaluate(ctx, 1.0)["净EV_元每注"]
    old = DS.evaluate(ctx, 1.0, legacy=True)["净EV_元每注"]
    assert old > main


# ---------------------------------------------------------------- ② 封顶
def test_first_prize_never_exceeds_cap_after_tax():
    ctx = _ctx()
    for d in (0.0, 0.5, 0.9, 0.99, 1.0):
        pay = DS.evaluate(ctx, d)["分奖级"]["一等"]["期望实得"]
        assert pay <= DS.CAP_DEFAULT + 1e-6
        assert DS.after_tax(pay) <= DS.after_tax(DS.CAP_DEFAULT) + 1e-6


def test_pool_error_scales_pool_not_payout():
    """★ 奖池 +30% 不许把封顶也乘掉（封顶是规则，不是误差项）。"""
    ctx = _ctx()
    pay = DS.evaluate(ctx, 1.0, pool_error=0.30)["分奖级"]["一等"]["期望实得"]
    assert pay <= DS.CAP_DEFAULT * 1.0000001


def test_delta_effect_is_tiny_when_cap_binds():
    """封顶绑定时 δ 的效力被压到极小（不是恰好为 0，而是被截断吃掉）。

    实测（双色球，池 1.1e8、mu≈9.9）：δ 从 0 到 1 只把净 EV 抬 ~0.036 元/注，
    而转正缺口 ~1 元/注 ⇒ δ 连缺口的 4% 都补不上。
    """
    ctx = _ctx()
    ev0 = DS.evaluate(ctx, 0.0)["净EV_元每注"]
    ev1 = DS.evaluate(ctx, 1.0)["净EV_元每注"]
    gain = ev1 - ev0
    assert 0.0 <= gain < 0.05, f"δ 的效力应被封顶压到极小，实测 {gain:+.4f}"
    assert ev1 < 0, "δ 到 1 仍为负 ⇒ 与「不可能转正」一致"


# ---------------------------------------------------------------- ③ 结构性上限
def test_cap_contribution_independent_of_pool_size():
    """上限 = P(一等) × 封顶税后，与奖池规模无关（这条是本模块的核心判决依据）。"""
    a = DS.structural_bound(_ctx(pool_scale=1.0))
    b = DS.structural_bound(_ctx(pool_scale=10.0))
    assert a["一等贡献_上限_元每注"] == pytest.approx(b["一等贡献_上限_元每注"], rel=1e-12)
    assert a["P_一等"] == pytest.approx(b["P_一等"], rel=1e-12)
    # 池越大，当前贡献越高 ⇒ 可争取的增量越小（单调）
    assert b["δ可争取总量_元每注"] <= a["δ可争取总量_元每注"] + 1e-12


def test_structural_bound_says_cannot_close_gap():
    """双色球：δ 最多能加的钱远小于转正缺口 ⇒ 结构性不可能转正。"""
    b = DS.structural_bound(_ctx())
    assert b["δ可争取总量_元每注"] < 1.0
    assert b["转正所需缺口_元每注"] > 1.0
    assert b["δ上限能否补上缺口"] is False
    assert "与" in b["_含义"] or "无论" in b["_含义"]


def test_structural_bound_second_prize_is_not_a_delta_lever():
    """二等不是 δ 的杠杆 —— 上限只由一等给出。"""
    b = DS.structural_bound(_ctx())
    assert b["一等贡献_上限_元每注"] == pytest.approx(
        b["P_一等"] * DS.after_tax(b["单注封顶"]), rel=1e-12)


# ---------------------------------------------------------------- ④ δ*
def test_delta_star_none_implies_never_positive():
    ctx = _ctx()
    ds = DS.delta_star(ctx)
    if ds is None:
        assert DS.evaluate(ctx, 1.0)["净EV_元每注"] < 0
    else:
        assert 0.0 <= ds <= 1.0
        assert DS.evaluate(ctx, ds)["净EV_元每注"] == pytest.approx(0.0, abs=1e-6)


def test_delta_star_zero_when_already_positive():
    ctx = _ctx(pool_scale=1e4)          # 极端假设：奖池放大 1 万倍
    if DS.evaluate(ctx, 0.0)["净EV_元每注"] > 0:
        assert DS.delta_star(ctx) == 0.0


# ---------------------------------------------------------------- ⑤ 网格
def test_grid_covers_full_range():
    g = DS.delta_grid(_ctx())
    assert g["情景数"] == len(DS.DELTA_GRID) * len(DS.POOL_ERROR_GRID) == len(g["行"])
    assert g["δ档"][0] == 0.0 and g["δ档"][-1] == 1.0
    assert g["正情景数"] == len(g["正情景"])
    assert g["口径"].startswith("主")


def test_grid_legacy_label():
    assert DS.delta_grid(_ctx(), legacy=True)["口径"].startswith("旧")


def test_after_tax_thresholds():
    assert DS.after_tax(10_000.0) == pytest.approx(10_000.0)
    assert DS.after_tax(20_000.0) == pytest.approx(18_000.0)
    assert DS.after_tax(0.0) == 0.0


def test_honest_note_says_assumption_and_interval():
    assert "假设" in DS.HONEST_NOTE and "区间" in DS.HONEST_NOTE
    assert "代理" in DS.HONEST_NOTE


# ---------------------------------------------------------------- ⑥ 真数据
@pytest.mark.realdata
def test_scenario_report_real_issue():
    from ev.tier_backtest import load_test_recs
    issue = load_test_recs("双色球")[-1].期号
    rep = DS.scenario_report("双色球", issue)
    for k in ("主口径", "旧口径", "结构性上限", "δ*_主口径", "诚实边界"):
        assert k in rep
    assert rep["主口径"]["情景数"] == 35
    # 旧口径的正情景不得少于主口径（它是高估）
    assert rep["旧口径"]["正情景数"] >= rep["主口径"]["正情景数"]
