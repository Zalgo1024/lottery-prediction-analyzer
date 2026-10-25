# -*- coding: utf-8 -*-
"""tests/test_arena_design.py —— 并集贪心的正确性与两个已知陷阱。"""
from __future__ import annotations

import numpy as np
import pytest

from ev.arena import design as DG
from ev.coverage_backtest import build_pool

TARGET = {"kind": "payout", "min_payout": 10.0}
LOT = "双色球"


def _pool(n=600):
    return build_pool(LOT, n, seed=20261024)


def test_outcome_gate_shape_and_inner_pool():
    """★ 内层池大小是 16（双色球）/ 12（大乐透），不是 `inner_size`（大乐透 66）。"""
    from ev.tier_backtest import load_test_recs
    from ev.profit.rules import rules_for

    issue = load_test_recs(LOT)[-1].期号
    g, w_out, m_in, n_in = DG.outcome_gate(LOT, TARGET, issue)
    assert w_out == 6 and m_in == 1 and n_in == 16
    assert g.shape == (7, 2)

    di = load_test_recs("大乐透")[-1].期号
    g2, w2, m2, n2 = DG.outcome_gate("大乐透", TARGET, di)
    assert w2 == 5 and m2 == 2 and n2 == 12
    assert g2.shape == (6, 3)


def test_sample_draws_shapes_and_uniformity():
    rng = np.random.default_rng(0)
    R, B = DG.sample_draws(rng, 33, 6, 16, 1, 4000)
    assert R.shape == (4000, 33) and B.shape == (4000, 16)
    assert np.all(R.sum(axis=1) == 6) and np.all(B.sum(axis=1) == 1)
    # 边际频次应接近均匀（4000×6/33 ≈ 727，3σ 内）
    exp = 4000 * 6 / 33
    freq = R.sum(axis=0)
    assert np.abs(freq - exp).max() < 4 * np.sqrt(exp)


def test_union_rate_monotone_in_selection():
    """加票不会降低并集覆盖率（次模/单调） ⇒ 贪心有界的前提。"""
    pool = _pool(300)
    rng = np.random.default_rng(1)
    R, B = DG.sample_draws(rng, 33, 6, 16, 1, 400)
    gate, _w, _m, _n = DG.outcome_gate(LOT, TARGET, 26121)
    masks = DG.outcome_masks(pool, LOT, R, B, gate)
    rates = [DG.union_rate(masks, list(range(i + 1))) for i in range(8)]
    assert all(b >= a - 1e-12 for a, b in zip(rates, rates[1:]))


def test_greedy_respects_cap_and_never_relaxes():
    """★ cap 是硬约束：宁可少出票也不放宽（tier_coverage 会自动放宽到 +4）。"""
    pool = _pool(800)
    ov = DG.overlap_matrix(pool, LOT)
    rng = np.random.default_rng(2)
    R, B = DG.sample_draws(rng, 33, 6, 16, 1, 300)
    gate, _w, _m, _n = DG.outcome_gate(LOT, TARGET, 26121)
    masks = DG.outcome_masks(pool, LOT, R, B, gate)
    sel, tr = DG.greedy_union(masks, ov, 60, cap=3)
    assert tr["放宽次数"] == 0
    assert tr["重叠上限"] == 3
    sub = ov[np.ix_(sel, sel)]
    np.fill_diagonal(sub, 0)
    assert sub.max() <= 3


def test_winners_curse_direction():
    """拟合覆盖率 ≥ 样本外覆盖率：同批采样上既优化又评估会高估。"""
    pool = _pool(800)
    tks, meta = DG.select_union_greedy(LOT, pool, 40, TARGET, 26121,
                                       m_fit=3000, m_eval=3000)
    assert meta["拟合覆盖率"] >= meta["样本外覆盖率"] - 1e-9
    assert meta["优化乐观量"] >= -1e-9


def test_select_union_greedy_deterministic():
    pool = _pool(400)
    a, _ = DG.select_union_greedy(LOT, pool, 20, TARGET, 26121,
                                  m_fit=2000, m_eval=2000, seed=5)
    b, _ = DG.select_union_greedy(LOT, pool, 20, TARGET, 26121,
                                  m_fit=2000, m_eval=2000, seed=5)
    assert [tuple(sorted(t["红球"])) for t in a] == [tuple(sorted(t["红球"])) for t in b]


def test_eval_stream_is_independent_of_fit_stream():
    """评估种子必须 ≠ 优化种子，否则「样本外」是假的。"""
    pool = _pool(300)
    _, meta = DG.select_union_greedy(LOT, pool, 20, TARGET, 26121,
                                     m_fit=1000, m_eval=1000, seed=5)
    assert meta["评估seed"] != meta["seed"]


def test_rejects_non_redblue_lottery():
    with pytest.raises(ValueError, match="红蓝型"):
        DG.select_union_greedy("排列三", _pool(50), 5, TARGET, None,
                               m_fit=200, m_eval=200)
