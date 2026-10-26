# -*- coding: utf-8 -*-
"""tests/test_arena_packing.py —— `ev/arena/packing.py` 的牙齿。

钉的是四件事：
1. 解析联合分布是**真分布**（总概率 1、边缘与二项/超几何式一致）；
2. `F[c,b]` 与**精确枚举**反解出来的两票同现概率一致（这是本模块的核心构件，
   它错了后面所有「并集优化」都是错的）；
3. 两票时「一阶近似」必须与精确枚举**相等**（没有高阶项），
   多票时必须**高估**（Bonferroni 一阶截断的性质）—— 两条方向相反，一起钉才有效；
4. cap 是硬约束：swap 不许把任何一对红球交集抬到 cap 以上；
   且 cap<3 在 8000 池上必然选不满（这条事实是 v1.7 登记里「cap 不是污染」的依据）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from ev.arena import packing as PK
from ev.coverage_backtest import build_pool
from ev.tier_coverage import exact_union_probability
from ev.tier_target import resolve_conditions, single_ticket_probability

TGT4 = {"kind": "grade", "grade": "四等"}
ISSUE = 26122


# ------------------------------------------------------------------ 分布本体
@pytest.mark.parametrize("n,k", [(33, 6), (35, 5)])
def test_joint_outer_is_a_distribution(n, k):
    for c in range(k + 1):
        j = PK._joint_outer(n, k, c)
        assert abs(sum(j.values()) - 1.0) < 1e-12, f"c={c} 总概率不为 1"
        marg = {}
        for (h1, _h2), p in j.items():
            marg[h1] = marg.get(h1, 0.0) + p
        for a in range(k + 1):
            want = math.comb(k, a) * math.comb(n - k, k - a) / math.comb(n, k)
            assert abs(marg.get(a, 0.0) - want) < 1e-12, f"c={c} h={a} 边缘不符"


@pytest.mark.parametrize("n_in,m", [(16, 1), (12, 2)])
def test_joint_inner_is_a_distribution(n_in, m):
    for b in range(m + 1):
        j = PK._joint_inner(n_in, m, b)
        assert abs(sum(j.values()) - 1.0) < 1e-12, f"b={b} 总概率不为 1"
        # 交集 b 时两票的内层命中数必须相等或至少同时 ≥ b 的结构约束：
        # 对称性 ⇒ P(j1=a, j2=c) == P(j1=c, j2=a)
        for (j1, j2), p in j.items():
            assert abs(j.get((j2, j1), 0.0) - p) < 1e-15, "内外层联合分布不对称"


# ------------------------------------------------------------------ F 表 vs 精确枚举
@pytest.mark.parametrize("lottery,K", [("双色球", 2), ("大乐透", 2)])
def test_pair_table_matches_exact_enumeration(lottery, K):
    """两票时 `P(A1∪A2) = 2p − P(A1∩A2)` ⇒ 反解出的同现率必须等于 `F[c,b]`。"""
    pool = build_pool(lottery, 60, seed=20261024)
    tgt = TGT4
    F, _d, _k, _m = PK.pair_hit_table(lottery, tgt, ISSUE)
    res = resolve_conditions(lottery, tgt, ISSUE)
    p1 = single_ticket_probability(lottery, res["conditions"], ISSUE)
    ov = PK.overlap_matrix(pool, lottery)
    r = PK.rules_for(lottery, ISSUE)
    from ev.tier_coverage import _inner_pool_size
    n_in = _inner_pool_size(r, max(j for (_h, j) in r.grade_table))
    bv = PK.inner_overlap_matrix(pool, lottery, n_in)

    checked = 0
    for i in range(0, 40):
        for j in range(i + 1, 40):
            if checked >= 6:
                break
            ex = exact_union_probability(lottery, [pool[i], pool[j]], target=tgt, issue=ISSUE)
            pair_exact = 2 * p1 - ex
            pair_table = float(F[int(ov[i, j]), int(bv[i, j])])
            assert abs(pair_exact - pair_table) < 1e-12, (
                f"{lottery} 票({i},{j}) c={ov[i, j]} b={bv[i, j]}: "
                f"精确反解 {pair_exact:.3e} != 表值 {pair_table:.3e}")
            checked += 1
    assert checked > 0


# ------------------------------------------------------------------ 一阶近似的方向
def test_analytic_union_exact_for_two_tickets():
    """两票没有高阶项 ⇒ 两两修正必须与精确枚举**相等**。"""
    lottery = "双色球"
    pool = build_pool(lottery, 30, seed=7)
    tks = [pool[0], pool[1]]
    F, _d, _k, _m = PK.pair_hit_table(lottery, TGT4, ISSUE)
    res = resolve_conditions(lottery, TGT4, ISSUE)
    p1 = single_ticket_probability(lottery, res["conditions"], ISSUE)
    ov = PK.overlap_matrix(pool, lottery)
    r = PK.rules_for(lottery, ISSUE)
    from ev.tier_coverage import _inner_pool_size
    n_in = _inner_pool_size(r, max(j for (_h, j) in r.grade_table))
    bv = PK.inner_overlap_matrix(pool, lottery, n_in)
    a = PK.analytic_union([0, 1], ov, bv, F, p1)["两两修正"]
    ex = exact_union_probability(lottery, tks, target=TGT4, issue=ISSUE)
    assert abs(a - ex) < 1e-12, f"两票时应相等：{a} vs {ex}"


def test_analytic_union_is_tight_lower_bound_for_many_tickets():
    """多票时两两修正是**二阶 Bonferroni 下界**：略低于精确，但极紧（相对误差 <0.1%）。"""
    lottery = "双色球"
    pool = build_pool(lottery, 2000, seed=20261024)
    F, _d, _k, _m = PK.pair_hit_table(lottery, TGT4, ISSUE)
    res = resolve_conditions(lottery, TGT4, ISSUE)
    p1 = single_ticket_probability(lottery, res["conditions"], ISSUE)
    ov = PK.overlap_matrix(pool, lottery)
    r = PK.rules_for(lottery, ISSUE)
    from ev.tier_coverage import _inner_pool_size
    n_in = _inner_pool_size(r, max(j for (_h, j) in r.grade_table))
    bv = PK.inner_overlap_matrix(pool, lottery, n_in)
    sel = list(range(60))
    a = PK.analytic_union(sel, ov, bv, F, p1)["两两修正"]
    ex = exact_union_probability(lottery, [pool[i] for i in sel], target=TGT4, issue=ISSUE)
    assert a <= ex + 1e-12, f"二阶下界不得高于精确：{a} vs {ex}"
    assert (ex - a) / ex < 1e-3, f"下界太松：相对差 {(ex - a) / ex:.2e}"


# ------------------------------------------------------------------ 选号器约束
def test_select_respects_cap_and_fills():
    lottery = "双色球"
    K = 24
    pool = build_pool(lottery, 3000, seed=20261024)
    tks, meta = PK.select_tier_packing(
        lottery, pool, K, TGT4, ISSUE, cap=3, seed=0,
        m_fit=20_000, m_eval=20_000, n_starts=1, n_init=0,
        swap_rounds=1, swap_candidates=16)
    assert len(tks) == K, f"出票 {len(tks)}/{K}"
    assert meta["放宽次数"] == 0 and meta["重叠上限"] == 3
    A = np.zeros((K, 33), dtype=np.int16)
    for i, t in enumerate(tks):
        for x in t["红球"]:
            A[i, int(x) - 1] = 1
    ov = A @ A.T
    np.fill_diagonal(ov, 0)
    assert int(ov.max()) <= 3, f"cap 被破坏：最大交集 {int(ov.max())}"


def test_cap_below_three_cannot_fill_the_pool():
    """★ 登记里「cap 不是污染」的事实依据：cap<3 在 8000 池上凑不满 200 注。"""
    from ev.selection import control_tickets
    pool = build_pool("双色球", 8000, seed=20261024)
    n1 = len(control_tickets("双色球", list(pool), 200, seed=0, cap=1))
    n2 = len(control_tickets("双色球", list(pool), 200, seed=0, cap=2))
    n3 = len(control_tickets("双色球", list(pool), 200, seed=0, cap=3))
    assert n3 == 200
    assert n1 < 100 and n2 < 150, f"cap1={n1} cap2={n2} cap3={n3}"


def test_swap_improves_analytic_target_or_keeps_it():
    """swap 只在目标严格变好时接受 ⇒ 后值必须 ≥ 前值。"""
    lottery = "双色球"
    pool = build_pool(lottery, 2000, seed=20261024)
    tks, meta = PK.select_tier_packing(
        lottery, pool, 30, TGT4, ISSUE, cap=3, seed=0,
        m_fit=20_000, m_eval=20_000, n_starts=1, n_init=0,
        swap_rounds=2, swap_candidates=32)
    assert meta["swap后覆盖率_解析"] >= meta["swap前覆盖率_解析"] - 1e-12
    assert len(tks) == 30


def test_eval_coverage_is_not_above_upper_bound():
    """样本外覆盖率不得超过 ΣᵢP(Aᵢ)（上界）—— 超了就是口径错。"""
    lottery = "大乐透"
    pool = build_pool(lottery, 2000, seed=20261024)
    _tks, meta = PK.select_tier_packing(
        lottery, pool, 20, TGT4, ISSUE, cap=3, seed=0,
        m_fit=20_000, m_eval=40_000, n_starts=1, n_init=0,
        swap_rounds=1, swap_candidates=8)
    assert meta["样本外覆盖率"] <= meta["覆盖率上界_S1"] + 4 * meta["覆盖率上界_SE"] + 1e-12
