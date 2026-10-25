# -*- coding: utf-8 -*-
"""tests/test_arena_replay.py —— 快路必须与审计路径逐票一致。

★ 这是本模块存在的理由：向量化判级比 `evaluate_period` 快约 9 倍
  （0.2 s/期 vs 1.8 s/期），但快路一旦与审计路径分叉，整个对照表的数就全废了。
  Stage 0 已做过 33,600 + 69,480 票的对账（0 处不一致）；这里把它固化成回归钉。
"""
from __future__ import annotations

import numpy as np
import pytest

import ev.tier_target as TT
from ev.arena import replay as RP
from ev.arena.design import _onehot, outcome_gate
from ev.coverage_backtest import build_pool
from ev.profit.rules import rules_for
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import _inner_pool_size

TARGET = {"kind": "payout", "min_payout": 10.0}
AUDIT_PERIODS = 12
AUDIT_TICKETS = 40


@pytest.mark.parametrize("lottery", ["双色球"])
def test_vectorized_matches_target_is_hit(lottery):
    """逐票、逐期对账：`gate[h, j]` vs `TT.target_is_hit`。"""
    pool = build_pool(lottery, 400, seed=20261024)
    recs = load_test_recs(lottery)[-AUDIT_PERIODS:]
    issue = recs[-1].期号
    r = rules_for(lottery, issue)
    _g, w_out, m_in, n_in = outcome_gate(lottery, TARGET, issue)

    A = _onehot(pool, "红球", int(r.outer_range[1]))
    B = _onehot(pool, "蓝球", n_in)

    bad = 0
    for rec in recs:
        # ★ 审计用**当期**规则解析出的 grades，不能把最新期的结果传进去
        #   （`target_is_hit` 见到 grades 键会忽略 issue ⇒ 静默用最新奖级）
        res_p = TT.resolve_conditions(lottery, TARGET, rec.期号)
        gate_p, _w, _m, _n = outcome_gate(lottery, TARGET, rec.期号)
        R, Bd = RP.draw_vectors(lottery, [rec], int(r.outer_range[1]), n_in)
        h = (A.astype(np.int16) @ R.T.astype(np.int16))[:, 0]
        j = (B.astype(np.int16) @ Bd.T.astype(np.int16))[:, 0]
        vec = gate_p[h, j]
        for i, t in enumerate(pool[:AUDIT_TICKETS]):
            dz = {"红球": rec.红球, "蓝球": rec.蓝球}
            ref = TT.target_is_hit(lottery, t, dz, rec.期号, res_p)
            if bool(vec[i]) != bool(ref):
                bad += 1
    assert bad == 0, f"快路与审计路径 {bad} 处不一致"


def test_replay_current_mode_equals_periodic_when_single_rule_version():
    """双色球只有 1 个规则版本 ⇒ 两种口径必须逐位相同。"""
    lottery = "双色球"
    pool = build_pool(lottery, 400, seed=20261024)
    recs = load_test_recs(lottery)[-60:]
    issue = recs[-1].期号
    tks = pool[:30]
    a = RP.replay(lottery, tks, recs, TARGET, rule_mode="current", target_issue=issue)
    b = RP.replay(lottery, tks, recs, TARGET, rule_mode="periodic", target_issue=issue)
    np.testing.assert_array_equal(a.hit, b.hit)
    np.testing.assert_array_equal(a.nhit, b.nhit)


def test_replay_rejects_unknown_rule_mode():
    pool = build_pool("双色球", 100, seed=1)
    recs = load_test_recs("双色球")[-5:]
    with pytest.raises(ValueError, match="rule_mode"):
        RP.replay("双色球", pool[:5], recs, TARGET, rule_mode="whatever")


def test_replay_issues_and_cost():
    pool = build_pool("双色球", 200, seed=1)
    recs = load_test_recs("双色球")[-30:]
    r = RP.replay("双色球", pool[:10], recs, TARGET, rule_mode="current")
    assert [str(x) for x in r.issues] == [str(x.期号) for x in recs]
    assert r.cost == pytest.approx(30 * 10 * 2.0)     # 期数 × 注数 × 2 元
    assert r.nhit.shape == (30,)
    assert r.hit.sum() <= 30


def test_replay_multi_version_lottery_differs_between_modes():
    """大乐透多版本 ⇒ current 与 periodic 不同。这是必须显式选择的口径，不能默认。

    ★ 用「四等」而不是「≥10元」：实测三个规则版本上「≥10元」的 `(h, j)` 达标门
      **恰好同构**（gate 非零元都是 9 个）⇒ 两种口径在该终点上退化成同一个数。
      也就是说「口径选择无关紧要」这个印象只在某些终点上成立，
      换到「四等」立刻暴露（旧版本 gate 4 个格、最新版 5 个格）。
    """
    lottery = "大乐透"
    target = {"kind": "grade", "grade": "四等"}
    pool = build_pool(lottery, 200, seed=1)
    recs = load_test_recs(lottery)
    issue = recs[-1].期号
    a = RP.replay(lottery, pool[:20], recs, target, rule_mode="current", target_issue=issue)
    b = RP.replay(lottery, pool[:20], recs, target, rule_mode="periodic", target_issue=issue)
    assert not np.array_equal(a.hit, b.hit), "多版本彩种的两种口径必须产生不同的达标序列"


def test_payout_target_gate_is_version_invariant_for_dlt():
    """把上面那条反向事实钉住：`≥10元` 在大乐透三个规则版本上 gate 同构。

    这不是「口径无关」的证明，只是说明**该终点测不出口径差**，
    所以不能拿它当「两种口径一致」的证据。
    """
    lottery = "大乐透"
    target = {"kind": "payout", "min_payout": 10.0}
    recs = load_test_recs(lottery)
    picks = (recs[0].期号, recs[500].期号, recs[-1].期号)
    gs = [outcome_gate(lottery, target, i)[0] for i in picks]
    for g in gs[1:]:
        np.testing.assert_array_equal(gs[0], g)

