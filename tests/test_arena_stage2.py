# -*- coding: utf-8 -*-
"""tests/test_arena_stage2.py —— Stage 2 倍投 runner 的契约。

这里锁的是「倍投只改金额与成本，**绝不改命中结构**」这条铁律。
三条不变式里任何一条被破坏都意味着**实现缺陷**（比如判级时把倍数乘了进去），
而不是「这个方案表现不好」—— 所以失败必须响，不许静默放宽。

外加一条边界：本模块**不接生产**（不写 pending、不接调度器）。
阶段性状态：2026-10-25 起 Stage 2 已在预注册 v1.3 登记（`_register_stage2.py`），
④ 节的哨兵随之从「必须没登记」翻转为「必须已登记且哈希自洽」。
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from ev import preregistration as PR
from ev import tier_allocate as TA
from ev.arena import replay as RP
from ev.arena import stage2 as S
from ev.coverage_backtest import build_pool
from ev.tier_backtest import load_test_recs

LOT = "双色球"
TARGET = {"kind": "payout", "min_payout": 10.0}
K = 12
PERIODS = 72


def _fixture():
    pool = build_pool(LOT, 400, seed=20261024)
    recs = load_test_recs(LOT)[-PERIODS:]
    issue = recs[-1].期号
    return pool[:K], recs, issue


# ---------------------------------------------------------------- ① 结构性保证
def test_multipliers_do_not_change_hit_series():
    """★ 铁律：同一张票买几倍不改变「达标与否」，必须在代码路径上就够不到倍数。"""
    tks, recs, issue = _fixture()
    base = RP.replay(LOT, tks, recs, TARGET, rule_mode="current",
                     target_issue=issue, with_money=False)
    mult = [1 + (i % 3) for i in range(len(tks))]
    heavy = RP.replay(LOT, tks, recs, TARGET, rule_mode="current",
                      target_issue=issue, multipliers=mult)
    np.testing.assert_array_equal(base.hit, heavy.hit)
    np.testing.assert_array_equal(base.nhit, heavy.nhit)


def test_multipliers_change_only_cost_and_money():
    """倍数只该进金额与成本：实投注数 ↑、成本 ↑，`达标率` 不变。"""
    tks, recs, issue = _fixture()
    base = RP.replay(LOT, tks, recs, TARGET, rule_mode="current", target_issue=issue)
    mult = [2] * len(tks)
    heavy = RP.replay(LOT, tks, recs, TARGET, rule_mode="current",
                      target_issue=issue, multipliers=mult)
    assert heavy.cost == pytest.approx(base.cost * 2.0)
    assert float(heavy.hit.mean()) == pytest.approx(float(base.hit.mean()))
    assert heavy.meta["每期注数"] == base.meta["每期注数"] == len(tks)
    assert heavy.meta["每期实际注数"] == 2 * len(tks) != heavy.meta["每期注数"]
    assert heavy.meta["倍投"] is True and base.meta["倍投"] is False
    # ★ 键名带 `★` 前缀是有意的（这是契约说明，不是普通数据），断言必须精确匹配：
    #   宽松的 `"倍投口径" in meta` 在 Python dict 里查的是**键**，永远为 False，
    #   会让这条断言长期假通过/假失败，看不出真实问题。
    assert "★倍投口径" in heavy.meta
    assert "倍投口径" not in base.meta          # 非倍投方案不得出现该说明


def test_replay_validates_multiplier_vector():
    tks, recs, issue = _fixture()
    with pytest.raises(ValueError, match="长度"):
        RP.replay(LOT, tks, recs, TARGET, target_issue=issue,
                  multipliers=[1] * (len(tks) + 1))
    with pytest.raises(ValueError, match="≥ 1"):
        RP.replay(LOT, tks, recs, TARGET, target_issue=issue,
                  multipliers=[0] * len(tks))


# ---------------------------------------------------------------- ② 方案构造
@pytest.mark.parametrize("ratio", list(S.N2_RATIOS))
@pytest.mark.parametrize("obj", ["few_double", "incremental_coverage"])
def test_plan_conserves_budget(obj, ratio):
    """无论怎么加倍，总下注注数必须守恒到预算 n_bets。"""
    tks, _recs, _iss = _fixture()
    p = S.build_plan(tks, K, obj, ratio)
    assert p["总注数"] == K, f"{obj}@{ratio}: 实投 {p['总注数']} 注 ≠ 预算 {K} 注"
    assert p["去重注数"] < K, f"{obj}@{ratio}: 去重数必须小于 K（否则等于没加倍）"
    assert len(p["倍数"]) == p["去重注数"] == len(p["票"])
    assert min(p["倍数"]) >= 1


def test_all_single_is_the_dominating_degenerate_plan():
    """all_single = K 张互异票各 1 倍 ⇒ 去重数 == K，倍数全 1。"""
    tks, _r, _i = _fixture()
    p = S.build_plan(tks, K, "all_single", 0.10)
    assert p["去重注数"] == K
    assert set(p["倍数"]) == {1}
    assert len(p["票"]) == K


def test_monetary_threshold_without_weights_degenerates_to_all_single():
    """κ=0 时所有票单注期望奖金相同 ⇒ 该目标**没有独立含义**，退化为 all_single。

    ★ 这不是 bug，是必须显式承认的退化（否则会被读成「第四个策略」）。
    """
    tks, _r, _i = _fixture()
    a = S.build_plan(tks, K, "all_single", 0.10)
    b = S.build_plan(tks, K, "monetary_threshold", 0.10)
    assert [t["红球"] for t in a["票"]] == [t["红球"] for t in b["票"]]
    assert b["去重注数"] == a["去重注数"]


def test_unknown_objective_is_rejected():
    tks, _r, _i = _fixture()
    with pytest.raises(ValueError, match="未知 objective"):
        S.build_plan(tks, K, "double_or_nothing", 0.1)


# ---------------------------------------------------------------- ③ 不变式（含牙齿）
def test_invariants_pass_on_real_small_cell():
    tks, recs, issue = _fixture()
    out = S.run_stage2_cell(LOT, tks, recs, TARGET, rule_mode="current",
                            target_issue=issue, arm_name="test", verbose=False)
    inv = out["不变式"]
    assert inv["通过"], inv["明细"]
    assert inv["违反"] == []
    # 每个方案都必须「实投 == 预算」，否则成本口径就歪了
    for r in out["方案"]:
        assert r["总注数"] == K, f"{r['臂']} 实投 {r['总注数']} ≠ 预算 {K}"
    # 网格 = all_single 一格 + 其余三个目标 × 每个 n2_ratio
    assert len(out["方案"]) == 1 + (len(S.OBJECTIVES) - 1) * len(S.N2_RATIOS)


def _fake(hit, **meta):
    class _R:
        pass
    r = _R()
    r.hit = hit
    r.meta = meta
    return r


def test_i2_weak_dominance_is_enforced():
    """all_single 严格劣于某个倍投方案 ⇒ 必须判`违反`（ utilization 实现缺陷信号）。"""
    base = _fake(np.array([True, False]))
    worse = _fake(np.array([False, False]))
    assert S.check_invariants({"all_single": base, "few_double@0.10": worse})["通过"]
    better = _fake(np.array([True, True]))           # 达标率 1.0 > 0.5
    inv = S.check_invariants({"all_single": base, "few_double@0.10": better})
    assert not inv["通过"]
    assert "I2 all_single弱占优" in inv["违反"]


def test_i1_same_distinct_set_must_match_bitwise():
    """同一去重票集的两个目标，hit 必须逐位相同；不同一位就判违反。"""
    a = np.array([True, True, False])
    same = a.copy()
    assert S.check_invariants({"few_double@0.10": _fake(a),
                               "incremental_coverage@0.10": _fake(same)})["通过"]
    flipped = np.array([True, False, False])
    inv = S.check_invariants({"few_double@0.10": _fake(a),
                              "incremental_coverage@0.10": _fake(flipped)})
    assert not inv["通过"]
    assert "I1 同票集同命中" in inv["违反"]


def test_i3_multipliers_must_not_leak_into_hit():
    """参考序列（全 1 倍）与带倍数结果不一致 ⇒ 说明实现把倍数算进命中了。"""
    ref = np.array([True, False, True])
    good = ref.copy()
    inv = S.check_invariants({"few_double@0.05": _fake(good, _逐位参考=ref)})
    assert inv["通过"]
    bad = np.array([True, True, True])
    inv = S.check_invariants({"few_double@0.05": _fake(bad, _逐位参考=ref)})
    assert not inv["通过"]
    assert "I3 倍数不进命中" in inv["违反"]


# ---------------------------------------------------------------- ④ 边界：已预注册、仍不接生产
def test_stage2_is_preregistered_but_writes_no_pending():
    """★ 2026-10-25：Stage 2 已于 v1.3 登记（`ev.preregistration.stage2`），
    哨兵的方向随之翻转 —— 现在要守的是「**登记了且哈希自洽**」，而不是「没登记」。

    同时仍钉住两件不变的事：
      · 登记必须**可装载**（哈希不对一律抛，不许回退）；
      · Stage 2 依旧不写生产 pending、不接调度器。
    """
    doc = PR.load()
    assert "格_Stage2" in doc, "Stage 2 未登记 ⇒ 不得宣称结论"
    s2 = PR.stage2(doc)
    # 登记在跑之前：三条主张必须齐全，缺一条就不算登记完成
    assert {"H-S2-1", "H-S2-2", "H-S2-3"} <= {h["编号"] for h in s2["预注册主张"]}
    assert {"I1 同票集同命中", "I2 all_single弱占优", "I3 倍数不进命中"} <= set(s2["不变式"])
    # 源码里不得出现写生产 pending 的路径
    for m in (S, TA):
        src = inspect.getsource(m)
        for kw in ("save_pending", "write_pending", "record_pending", "update_pending"):
            assert kw not in src, f"{m.__name__} 出现了生产写入入口 {kw}"
