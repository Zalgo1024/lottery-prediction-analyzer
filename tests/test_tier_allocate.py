# -*- coding: utf-8 -*-
"""tests/test_tier_allocate.py —— 倍投分配层的契约与「可证明的弱占优」。

核心那条（模块文档第 1 条）在这里被写成**可执行断言**：
同一批去重票的**子集**，达标概率不可能高于全集（倍数不增加任何被覆盖的结局）。
"""
import pytest

from ev import tier_target as TT
from ev.coverage_backtest import build_pool
from ev.tier_allocate import (N2_RATIOS, OBJECTIVES, _max_drawdown,
                              allocate_multipliers, evaluate_period_weighted,
                              run_alloc_arm)
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import select_tier_coverage

LOT, ISSUE = "双色球", "26120"


@pytest.fixture(scope="module")
def tickets():
    pool = build_pool(LOT, 400, seed=0)
    tks, _meta = select_tier_coverage(LOT, pool, 40, "四等", ISSUE, seed=0)
    return tks


def test_all_single_is_fully_diversified(tickets):
    a = allocate_multipliers(tickets, 40, "all_single")
    assert a["总注数"] == 40 and a["去重注数"] == 40
    assert a["倍数分布"] == {1: 40}


@pytest.mark.parametrize("ratio", N2_RATIOS)
def test_few_double_keeps_budget_and_reduces_distinct(tickets, ratio):
    a = allocate_multipliers(tickets, 40, "few_double", n2_ratio=ratio)
    assert a["总注数"] == 40, "预算必须守恒"
    m = 40 - a["去重注数"]
    assert m >= 1
    assert a["倍数分布"].get(2, 0) == m
    assert sum(k * v for k, v in a["倍数分布"].items()) == 40


def test_few_double_doubles_the_head(tickets):
    """few_double 加给「边际最大的」（顺序靠前）⇒ 前 m 张倍数 2。"""
    a = allocate_multipliers(tickets, 40, "few_double", n2_ratio=0.10)
    m = 40 - a["去重注数"]
    assert [mm for _t, mm in a["票"][:m]] == [2] * m
    assert all(mm == 1 for _t, mm in a["票"][m:])


def test_incremental_coverage_doubles_the_tail(tickets):
    """对照目标刻意加给最边际的票（顺序靠后）—— 预期结果更差，用来看代价。"""
    a = allocate_multipliers(tickets, 40, "incremental_coverage", n2_ratio=0.10)
    m = 40 - a["去重注数"]
    assert all(mm == 1 for _t, mm in a["票"][:40 - 2 * m])
    assert [mm for _t, mm in a["票"][-m:]] == [2] * m
    assert "刻意" in a["说明"]


def test_monetary_threshold_degenerates_without_weights(tickets):
    a = allocate_multipliers(tickets, 40, "monetary_threshold")
    assert a["去重注数"] == 40 and a["倍数分布"] == {1: 40}
    assert "退化" in a["说明"]


def test_bad_inputs(tickets):
    with pytest.raises(ValueError):
        allocate_multipliers(tickets, 40, "nope")
    with pytest.raises(ValueError):
        allocate_multipliers(tickets, 0, "all_single")
    with pytest.raises(ValueError):
        allocate_multipliers(tickets[:5], 40, "few_double")


# ---------------------------------------------------------------- 结算口径
def test_multiplier_does_not_change_hit_but_scales_payout(tickets):
    """★ 倍数只放大实得，不改变达标；也不改变「中奖注数」以外的选号语义。"""
    recs = load_test_recs(LOT, 25)
    grades = TT.resolve_conditions(LOT, "四等", ISSUE)["grades"]
    cache: dict = {}
    p1 = [(t, 1) for t in tickets[:20]]
    p2 = [(t, 2) for t in tickets[:20]]
    for rec in recs:
        e1 = evaluate_period_weighted(LOT, p1, rec, rec.期号, cache, grades)
        e2 = evaluate_period_weighted(LOT, p2, rec, rec.期号, cache, grades)
        assert e1["达标"] == e2["达标"], "重复票不得改变达标判定"
        assert e2["实得"] == pytest.approx(e1["实得"] * 2.0, abs=0.02)
        assert e2["投注注数"] == 2 * e1["投注注数"]
        assert e2["成本"] == pytest.approx(e1["成本"] * 2.0, abs=0.02)


def test_subset_cannot_beat_superset(tickets):
    """可证明的弱占优：few_double 的去重票集是 all_single 的子集 ⇒ 达标期数不更高。"""
    recs = load_test_recs(LOT, 40)
    cache: dict = {}
    a = allocate_multipliers(tickets, 40, "all_single")["票"]
    b = allocate_multipliers(tickets, 40, "few_double", n2_ratio=0.20)["票"]
    hit_a = hit_b = 0
    for rec in recs:
        ga = TT.resolve_conditions(LOT, "四等", rec.期号)["grades"]
        if evaluate_period_weighted(LOT, a, rec, rec.期号, cache, ga)["达标"]:
            hit_a += 1
        if evaluate_period_weighted(LOT, b, rec, rec.期号, cache, ga)["达标"]:
            hit_b += 1
    assert hit_b <= hit_a, (hit_b, hit_a)
    assert len(b) < len(a)


def test_max_drawdown():
    mdd, ln = _max_drawdown([10, -5, -5, -5, 30, -1])
    assert mdd == pytest.approx(15.0)
    assert ln >= 3


# ---------------------------------------------------------------- 回放
def test_run_alloc_arm_shape():
    r = run_alloc_arm(LOT, "all_single", 20, target="四等", periods=20, seed=0,
                      pool_size=300, reselect_every=10)
    assert r["期数"] == 20
    for k in ("达标期占比", "盈利期占比", "达标期数/100元", "净收益/100元",
              "最大回撤", "最大回撤持续期数", "单期最大投入", "单次大奖占总实得比",
              "序列_净额", "序列_实得", "序列_达标"):
        assert k in r, f"缺 {k}"
    assert len(r["序列_达标"]) == 20
    assert r["预算注数"] == 20


def test_cross_objective_budget_is_conserved():
    """跨目标比较的前提：总投注注数必须一致（预算守恒），否则比的是预算不是分配。"""
    runs = {}
    for o in OBJECTIVES:
        runs[o] = run_alloc_arm(LOT, o, 20, target="四等", periods=20, seed=0,
                                pool_size=300, reselect_every=10)
    staked = {o: r["总投注注数"] for o, r in runs.items()}
    assert len(set(staked.values())) == 1, staked
    assert set(OBJECTIVES) == {"all_single", "few_double", "incremental_coverage",
                               "monetary_threshold"}
    assert "不主张" in runs["all_single"]["口径提示"] or "只看去重票集" in runs["all_single"]["口径提示"]
