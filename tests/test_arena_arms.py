# -*- coding: utf-8 -*-
"""tests/test_arena_arms.py —— 臂注册表、RNG 隔离、cap 污染标记。"""
from __future__ import annotations

import numpy as np
import pytest

from ev.arena import arms as A
from ev.coverage_backtest import build_pool

POOL_SEED = 20261024
SMALL = 600


def _pool():
    return build_pool("双色球", SMALL, seed=POOL_SEED)


def test_registry_has_six_arms():
    assert set(A.ARMS) == {"random", "tier_coverage", "union_greedy",
                           "lowcrowd", "prod_mix", "hybrid"}


def test_unknown_arm_raises():
    with pytest.raises(KeyError):
        A.select_arm("nope", "双色球", _pool(), 50)


def test_derive_seed_is_deterministic_and_arm_specific():
    assert A.derive_seed(0, "random") == A.derive_seed(0, "random")
    assert A.derive_seed(0, "random") != A.derive_seed(0, "union_greedy")
    assert A.derive_seed(1, "random") != A.derive_seed(0, "random")


def test_isolated_global_rng_restores_state():
    before = np.random.get_state()[1][:5].tolist()
    with A.isolated_global_rng(12345):
        _ = np.random.random(3)
    assert np.random.get_state()[1][:5].tolist() == before


def test_random_arm_fills_k_and_uses_default_cap():
    pool = _pool()
    tks, meta = A.select_arm("random", "双色球", pool, 40, cap=3)
    assert len(tks) == 40
    assert meta["重叠上限"] == 3
    assert meta["cap污染"] is False


def test_select_arm_is_reproducible():
    """同一 (seed, 臂, K) 两次调用必须给出同一票集。"""
    pool = _pool()
    t1, _ = A.select_arm("random", "双色球", pool, 30, seed=7)
    t2, _ = A.select_arm("random", "双色球", pool, 30, seed=7)
    assert [tuple(sorted(t["红球"])) for t in t1] == [tuple(sorted(t["红球"])) for t in t2]


def test_union_greedy_arm_marks_no_relaxation():
    """★ 并集贪心**不允许**自动放宽 cap（tier_coverage 会，那是 G3 污染源）。"""
    pool = _pool()
    tks, meta = A.select_arm("union_greedy", "双色球", pool, 40,
                             {"kind": "payout", "min_payout": 10.0},
                             params={"m_fit": 5000, "m_eval": 5000})
    assert meta["放宽次数"] == 0
    assert meta["重叠上限"] == 3
    assert meta["样本外覆盖率"] <= meta["拟合覆盖率"] + 1e-12   # winner's curse 方向


def test_tier_coverage_meta_reports_relaxation_field():
    pool = _pool()
    tks, meta = A.select_arm("tier_coverage", "双色球", pool, 40,
                             {"kind": "payout", "min_payout": 10.0})
    assert "放宽次数" in meta and "重叠上限" in meta


@pytest.mark.statsmodels
def test_hybrid_arm_selects_full_k_on_ssq():
    """v1.4：hybrid 在双色球上必须真出数（λ=0.5 / union 锁定），不是「未实现」。"""
    pool = _pool()
    tks, meta = A.select_arm("hybrid", "双色球", pool, 40,
                             {"kind": "payout", "min_payout": 10.0},
                             params={"m_fit": 5000})
    assert len(tks) == 40
    assert str(meta["选择器"]).startswith("select_joint")
    assert meta["λ"] == 0.5 and meta["覆盖通道"] == "union"
    assert meta["重叠上限"] == 3 and meta["放宽次数"] == 0
    assert meta["cap污染"] is False
    assert meta["拥挤度闸门"] == "可用"
    assert (meta["门控"] or {})["请求分位"] == 0.30
    assert (meta["联合位图"] or {})["MC规模"] == 5000


@pytest.mark.statsmodels
def test_hybrid_arm_is_reproducible():
    """同 seed 两次调用必须同票集（RNG 隔离 + 派生 seed 真的接进去了）。"""
    pool = _pool()
    a, _ = A.select_arm("hybrid", "双色球", pool, 40,
                        {"kind": "payout", "min_payout": 10.0}, seed=7,
                        params={"m_fit": 5000})
    b, _ = A.select_arm("hybrid", "双色球", pool, 40,
                        {"kind": "payout", "min_payout": 10.0}, seed=7,
                        params={"m_fit": 5000})
    assert [tuple(sorted(t["红球"])) for t in a] == [tuple(sorted(t["红球"])) for t in b]


def test_hybrid_degrades_to_explicit_state_when_proxy_unavailable():
    """大乐透拥挤度代理未达 Go 门槛 ⇒ 抛 NotImplementedError（暂不评估），不是返回空票。"""
    pool = build_pool("大乐透", SMALL, seed=POOL_SEED)
    with pytest.raises(NotImplementedError) as ei:
        A.select_arm("hybrid", "大乐透", pool, 20,
                     {"kind": "payout", "min_payout": 10.0})
    assert "暂不评估" in str(ei.value) or "拥挤度" in str(ei.value)


def test_hybrid_empty_result_raises_not_silent(monkeypatch):
    """空票集必须**显式降级**，不许静默按 0 注结算（0 注回放 = 假 0 达标率）。"""
    from ev import lowcrowd_select as ls

    def _empty(*a, **k):
        return [], {"选择器": "select_joint（覆盖 + 冷门 联合目标）",
                    "降级": "测试用降级原因"}

    monkeypatch.setattr(ls, "select_joint", _empty)
    with pytest.raises(NotImplementedError, match="暂不评估"):
        A.select_arm("hybrid", "双色球", _pool(), 10,
                     {"kind": "payout", "min_payout": 10.0})


@pytest.mark.statsmodels
def test_hybrid_gate_relaxation_is_not_cap_pollution(monkeypatch):
    """★ 门控放宽 ≠ cap 污染：放宽记在「门控.放宽次数」，顶层「放宽次数」恒 0。

    否则 select_arm 的 cap污染 判据会把「门控多放宽一档」误报成「重叠上限被污染」，
    触发停止规则第 3 条、把 hybrid 打成「只能作诊断」。
    """
    from ev import lowcrowd_select as ls

    def _relaxed(*a, **k):
        tks, _ = A.select_arm("random", "双色球", a[1], a[2], cap=a[5] if len(a) > 5 else 3)
        return tks, {"选择器": "select_joint（覆盖 + 冷门 联合目标）",
                     "门控": {"请求分位": 0.3, "生效分位": 0.5, "放宽次数": 3,
                              "门控前候选数": 600, "门控后候选数": 500, "门控后容量": 40},
                     "覆盖": {"选中覆盖键": 120, "理论上限": 280, "目标奖级": "≥10 元"}}

    monkeypatch.setattr(ls, "select_joint", _relaxed)
    tks, meta = A.select_arm("hybrid", "双色球", _pool(), 40,
                             {"kind": "payout", "min_payout": 10.0})
    assert len(tks) == 40
    assert meta["放宽次数"] == 0 and meta["cap污染"] is False
    assert meta["门控"]["放宽次数"] == 3


def test_tier_coverage_trace_passthrough():
    """trace 透传：开了才有「贪心轨迹」键，且步数 == 选中注数。"""
    pool = _pool()
    _tks, meta = A.select_arm("tier_coverage", "双色球", pool, 40,
                              {"kind": "payout", "min_payout": 10.0},
                              params={"trace": True})
    tr = meta.get("贪心轨迹")
    assert tr is not None
    assert tr["汇总"]["步数"] == 40 == len(tr["逐步"])


def test_tier_coverage_no_trace_key_by_default():
    """trace 关时**不出现**「贪心轨迹」键（哨兵锁的就是这个）。"""
    pool = _pool()
    _tks, meta = A.select_arm("tier_coverage", "双色球", pool, 40,
                              {"kind": "payout", "min_payout": 10.0})
    assert "贪心轨迹" not in meta


def test_runner_arm_params_turns_trace_on():
    """runner 必须把诊断开关传进 params（纯诊断，不进预注册哈希）。"""
    from ev.arena.runner import _arm_params

    p = _arm_params({"MC拟合采样数": 100000, "MC评估采样数": 100000,
                     "评估seed偏移": 10000}, 3518)
    assert p["trace"] is True and p["m_fit"] == 100000 and p["n_periods"] == 3518


def test_lowcrowd_degrades_to_explicit_state_when_proxy_unavailable():
    """大乐透拥挤度代理不支持 ⇒ 抛 NotImplementedError（暂不评估），不是返回空票。"""
    pool = build_pool("大乐透", SMALL, seed=POOL_SEED)
    with pytest.raises(NotImplementedError) as ei:
        A.select_arm("lowcrowd", "大乐透", pool, 20,
                     {"kind": "payout", "min_payout": 10.0})
    assert "暂不评估" in str(ei.value) or "拥挤度" in str(ei.value)


def test_shortfall_is_recorded_not_silently_accepted():
    """凑不满 K 时必须留 `出票不足` 字段，而不是静默返回短票集。"""
    pool = _pool()
    _tks, meta = A.select_arm("random", "双色球", pool, 10_000, cap=3)
    assert "出票不足" in meta
    assert meta["实际注数"] < meta["要求注数"]
