# -*- coding: utf-8 -*-
"""tests/test_tier_coverage.py —— 奖级达标覆盖选号的契约与守卫。

钉死四件事：
1. 出号数量 == K（否则成本口径被污染，跨臂不可比）；
2. 重叠上限真的被遵守（用 `ev.selection._overlap` 独立复核，不信内部计数）；
3. 通道权重和是**上界**：必须 ≥ 精确概率，且不许被当成估计值输出；
4. `exact_portfolio_coverage` 在 K=1 时必须等于解析单注概率。
"""
import numpy as np
import pytest

from ev import tier_target as TT
from ev.coverage_backtest import build_pool
from ev.lowcrowd_backtest import PROD_OVERLAP_CAP
from ev.selection import _overlap
from ev.tier_coverage import exact_portfolio_coverage, select_tier_coverage

LOT = "双色球"
ISSUE = "26120"


@pytest.fixture(scope="module")
def pool():
    return build_pool(LOT, 400, seed=0)


@pytest.fixture(scope="module")
def sel(pool):
    return select_tier_coverage(LOT, pool, 30, "四等", ISSUE, seed=0)


def test_returns_exactly_k_from_pool(pool, sel):
    tickets, meta = sel
    assert len(tickets) == 30 == meta["选中注数"]
    ids = {id(t) for t in pool}
    assert all(id(t) in ids for t in tickets), "选出的票必须来自候选池（不得新造）"


def test_cap_is_respected(sel):
    """独立复核红球重叠（不信内部计数）。"""
    tickets, meta = sel
    cap = meta["重叠上限"]
    assert cap >= PROD_OVERLAP_CAP
    for i in range(len(tickets)):
        for j in range(i + 1, len(tickets)):
            assert _overlap(tickets[i], tickets[j], LOT) <= cap


def test_default_cap_is_production_three(pool):
    """默认重叠上限必须是生产口径 3，不是 `_OVERLAP_CAP["双色球"]=4`。"""
    _t, meta = select_tier_coverage(LOT, pool, 20, "四等", ISSUE, seed=0)
    assert meta["初始重叠上限"] == PROD_OVERLAP_CAP == 3
    from ev.selection import _OVERLAP_CAP

    assert _OVERLAP_CAP["双色球"] == 4, "本锁的前提：ev.selection 默认是 4，必须显式传 3"


def test_upper_bound_is_an_upper_bound(pool, sel):
    """Σ通道权重 ≥ 精确概率（上界性质），且 meta 明确标注它不是估计值。"""
    tickets, meta = sel
    exact = exact_portfolio_coverage(LOT, tickets, "四等", ISSUE)
    assert meta["通道权重和"] >= exact - 1e-9, (meta["通道权重和"], exact)
    assert meta["并集概率上界"] >= exact - 1e-9
    assert "上界" in meta["通道权重口径"]
    assert "上界" in meta["说明"]


def test_meta_flags_vacuous_bound():
    """K 大 + 目标松时上界会失效，meta 必须显式标出来（不许默默给个 2.0 当结论）。"""
    p = build_pool(LOT, 400, seed=1)
    _t, meta = select_tier_coverage(LOT, p, 200, "五等", ISSUE, seed=0)
    assert meta["理论上限Kp"] >= 1.0
    assert meta["上界是否有效"] is False
    assert meta["上界失效原因"]


def test_deterministic(pool):
    a, _ = select_tier_coverage(LOT, pool, 20, "四等", ISSUE, seed=7)
    b, _ = select_tier_coverage(LOT, pool, 20, "四等", ISSUE, seed=7)
    assert [t["红球"] for t in a] == [t["红球"] for t in b]


def test_capacity_shortfall_is_reported():
    """池子太小 / cap 太紧时必须如实报告，不许静默返回不足 K 注。"""
    small = build_pool(LOT, 60, seed=0)
    tickets, meta = select_tier_coverage(LOT, small, 200, "四等", ISSUE, seed=0)
    assert len(tickets) < 200
    assert "容量不足" in meta


def test_channel_count_bounds(pool, sel):
    tickets, meta = sel
    per = len(TT.coverage_keys_of(tickets[0], LOT, meta["条件集"], ISSUE))
    assert meta["覆盖通道数"] <= 30 * per


@pytest.mark.parametrize("lottery,issue", [("双色球", "26120"), ("大乐透", "26120")])
def test_exact_single_ticket_matches_analytic(lottery, issue):
    """K=1 ⇒ 精确并集概率必须等于解析单注概率。"""
    if lottery == "双色球":
        t = {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}
    else:
        t = {"红球": [1, 2, 3, 4, 5], "蓝球": [1, 2]}
    conds = TT.grade_conditions_at_or_above(lottery, "四等", issue)
    p = TT.single_ticket_probability(lottery, conds, issue)
    assert exact_portfolio_coverage(lottery, [t], "四等", issue) == pytest.approx(p, rel=1e-9)


def test_exact_grows_with_better_coverage():
    """交换律终检：同 K 下，覆盖更分散的一组必须给出更高的精确达标概率。"""
    rng = np.random.default_rng(0)
    base = build_pool("大乐透", 600, seed=2)
    # ① 三注「挤在同一片红球」 ② 三注「红球互不相交」
    tight = [{"红球": [1, 2, 3, 4, 5], "蓝球": [1, 2]},
             {"红球": [1, 2, 3, 4, 6], "蓝球": [1, 2]},
             {"红球": [1, 2, 3, 4, 7], "蓝球": [1, 2]}]
    spread = [{"红球": [1, 2, 3, 4, 5], "蓝球": [1, 2]},
              {"红球": [6, 7, 8, 9, 10], "蓝球": [1, 2]},
              {"红球": [11, 12, 13, 14, 15], "蓝球": [1, 2]}]
    p_tight = exact_portfolio_coverage("大乐透", tight, "四等", "26120")
    p_spread = exact_portfolio_coverage("大乐透", spread, "四等", "26120")
    assert p_spread > p_tight, (p_spread, p_tight)
    assert len(base) == 600  # 池构造正常（防呆）


def test_empty_conditions_raise():
    with pytest.raises(ValueError):
        select_tier_coverage("福彩3D", build_pool("福彩3D", 50, seed=0), 5,
                             {"kind": "grade", "grade": "组选3"})
