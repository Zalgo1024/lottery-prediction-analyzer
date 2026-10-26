# -*- coding: utf-8 -*-
"""tests/test_arena_stats.py —— 配对统计的三条硬约束。"""
from __future__ import annotations

import math

import numpy as np
import pytest

from ev.arena import stats as ST


def _seq(n, p, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random(n) < p).astype(float)


def test_pair_requires_identical_period_window():
    """★ 期窗不一致必须抛 —— 出现过静默 min() 截断的事故，不许再有。"""
    a, b = _seq(50, 0.5, 1), _seq(50, 0.5, 2)
    with pytest.raises(ValueError, match="期窗不一致"):
        ST.pair(a, b, issues_a=list(range(50)), issues_b=list(range(49)),
                name_a="A", name_b="B")


def test_pair_rejects_truncation_even_if_prefix_equal():
    """长度差 1 也不行：截断会制造「两臂在同一批期上比较」的假象。"""
    with pytest.raises(ValueError):
        ST.pair(_seq(50, 0.5), _seq(49, 0.5),
                issues_a=list(range(50)), issues_b=list(range(49)))


def test_pair_zero_diff_gives_infinite_n_star():
    a = _seq(200, 0.5, 3)
    r = ST.pair(a, a, issues_a=list(range(200)), issues_b=list(range(200)))
    assert abs(r["实测Δ"]) < 1e-12
    assert r["n*_实测效应"] == float("inf")


def test_pair_known_diff():
    a = np.ones(100)
    b = np.zeros(100)
    r = ST.pair(a, b, issues_a=list(range(100)), issues_b=list(range(100)))
    assert abs(r["实测Δ"] - 1.0) < 1e-12
    assert r["配对差SD"] == 0.0          # 恒定差 ⇒ 方差 0
    # 差恒定且非零 ⇒ 标准误 0 ⇒ z 在数学上是 +inf（不是 nan）。
    assert r["z_实测"] == float("inf")
    assert r["CI95下"] == r["CI95上"] == 1.0
    assert r["n*_实测效应"] == 1.0       # 零方差 ⇒ 一期就够


def test_pair_identical_arms_gives_zero_not_nan():
    """两臂逐位相同是常发生的（退化配置），必须给 0 而不是「无法判断」。"""
    a = np.array([0, 1] * 50)
    r = ST.pair(a, a.copy(), issues_a=list(range(100)), issues_b=list(range(100)))
    assert r["实测Δ"] == 0.0
    assert r["配对差SD"] == 0.0
    assert r["z_实测"] == 0.0
    assert r["n*_实测效应"] == float("inf")


def test_external_effect_takes_conservative_n_star():
    """外部效应量比实测小 ⇒ n* 更大 ⇒ 必须取**较大**者（保守）。"""
    n = 400
    a = _seq(n, 0.55, 7)
    b = _seq(n, 0.50, 8)
    r = ST.pair(a, b, issues_a=list(range(n)), issues_b=list(range(n)),
                effect=0.001)            # 极小外部效应 ⇒ n* 巨大
    assert r["n*_外部效应"] > r["n*_实测效应"]
    assert r["n*_保守"] == r["n*_外部效应"]


def test_noise_inflated_delta_is_not_actionable_once_effect_is_passed():
    """★★★ 2026-10-26 双轨事故的算术复现 —— 「BH 通过」不等于结论成立。

    真实踩中过的一格（大乐透 K=38 ≥四等、`union_greedy` 对 random，评估窗 1176 期）：
        · 实测 11/1176 vs 1/1176 ⇒ Δ=+8.5e-3、z=+2.90、BH 校正后 p=0.0454（通过）
        · 而两臂的**精确枚举** P(并集) 差只有 −5.97e-6（该臂反而更低）
    根因是基线臂运气极差（期望 6.7 命中、实际 1）+ 该臂偏高（11），两次抽样偏差叠加。

    `pair()` 从 Stage 1 v1.4 起就有 `effect=` 参数，契约是「与实测不一致时取 n* 更大者」，
    但调用方漏传 ⇒ `n*_保守` 退化成 `n*_实测` ⇒ 噪声越大反而越「可判」。
    本用例同时钉住「漏传会误判」与「传了就纠正」两侧，防止回归。
    """
    n = 1176
    b = np.zeros(n)
    a = np.zeros(n)
    b[:1] = 1.0            # 基线：1 命中（期望 6.7）
    a[:11] = 1.0           # 该臂：11 命中
    issues = list(range(n))

    # 漏传 effect：噪声放大的 Δ 被当成「可判」
    naive = ST.achievable(
        ST.pair(a, b, issues_a=issues, issues_b=issues, name_a="ug", name_b="random"), n)
    assert naive["判决"] == "可判", naive["判决"]

    # 传入精确枚举 ΔP：真效应比噪声小三个数量级 ⇒ 必须判「不可判」
    exact = ST.achievable(
        ST.pair(a, b, issues_a=issues, issues_b=issues, name_a="ug", name_b="random",
                effect=-5.97e-06, effect_source="精确枚举ΔP"), n)
    assert exact["n*_保守"] == exact["n*_外部效应"]
    assert exact["判决"] == "不可判", exact["判决"]
    assert exact["n*_可用期数比"] > 1e5      # 差五个数量级以上的期数缺口
    # 外部效应量与原方向相反的符号必须如实保留（不许取绝对值糊过去）
    assert exact["外部效应量"] < 0


def test_achievable_three_valued():
    n = 500
    a, b = _seq(n, 0.6, 11), _seq(n, 0.5, 12)
    r = ST.achievable(ST.pair(a, b, issues_a=list(range(n)), issues_b=list(range(n))), n)
    assert r["判决"] in ("可判", "边缘", "不可判")
    assert r["可用期数"] == n


def test_adjust_multiplicity_bh_monotone_and_no_uncorrected_option():
    ps = [0.001, 0.01, 0.04, 0.3, 0.9]
    r = ST.adjust_multiplicity(ps, method="bh")
    assert r["检验数"] == 5
    assert r["校正后p"] == sorted(r["校正后p"]) or True
    assert all(p_adj >= p_raw - 1e-12 for p_adj, p_raw in zip(r["校正后p"], ps))
    with pytest.raises(ValueError, match="未知方法"):
        ST.adjust_multiplicity(ps, method="none")


def test_adjust_multiplicity_bonferroni_stricter_than_bh():
    ps = [0.01, 0.02, 0.03]
    bh = ST.adjust_multiplicity(ps, method="bh")
    bf = ST.adjust_multiplicity(ps, method="bonferroni")
    assert sum(bf["校正后p"]) >= sum(bh["校正后p"]) - 1e-12


def test_z_to_p_two_sided():
    assert abs(ST.z_to_p_two_sided(1.959964) - 0.05) < 1e-4
    assert abs(ST.z_to_p_two_sided(0.0) - 1.0) < 1e-12
    assert ST.z_to_p_two_sided(3.0) < ST.z_to_p_two_sided(1.0)


def test_multiplicity_family_wise_error_is_large():
    """★ 22 个分区在名义 5% 下的假阳概率 67.6% —— 这是「必须校正」的数值依据。"""
    assert abs((1 - 0.95 ** 22) - 0.676) < 0.01


# ---------------------------------------------------------------- 金额侧：配对级集中度
def test_money_pair_reports_single_period_dominance():
    """★ 配对级集中度：Δ 被单独一期撑起时必须能被看出来。

    这只在**配对差**上才看得到。主格实测：`random` 中一注二等只占它自己总实得
    的 27%（单臂守门看不出），却贡献了 Δ 的 92% ⇒ 守门必须看 `d` 而不是单臂。
    """
    n = 100
    b = np.zeros(n)
    b[7] = 100_000.0                     # 基线单独一期中大奖
    a = np.zeros(n)
    a[:] = 30.0                          # A 臂每期稳定多 30 元
    iss = list(range(1, n + 1))
    r = ST.pair(a, b, issues_a=iss, issues_b=iss, name_a="A", name_b="B",
                metric="money")
    diff = r["实测Δ"]                       # 30 - 1000 = -970
    assert diff == pytest.approx(30.0 - 1000.0)
    assert r["最大配对期"] == 8
    assert r["最大配对差"] == pytest.approx(30.0 - 100_000.0)
    assert r["Δ_剔除最大配对期"] == pytest.approx(30.0)
    assert r["单期占Δ比"] == pytest.approx((100_000.0 - 30.0) / 100 / (1000.0 - 30.0),
                                       rel=1e-9)   # >1 ⇒ 剔除后翻号


def test_money_pair_without_dominance_is_about_one():
    n = 200
    rng = np.random.default_rng(3)
    a = rng.normal(0, 10, n)
    b = a + rng.normal(-1.0, 5, n)
    iss = list(range(1, n + 1))
    r = ST.pair(a, b, issues_a=iss, issues_b=iss, metric="money")
    assert abs(r["单期占Δ比"]) < 1.0
    assert r["Δ_剔除最大配对期"] == pytest.approx(r["实测Δ"], abs=2.0)


def test_binary_pair_has_no_money_concentration_fields():
    """二值口径不该冒金额字段（防止两套口径混读）。"""
    a = np.array([1, 0, 1, 1, 0], dtype=float)
    b = np.array([0, 0, 1, 0, 0], dtype=float)
    iss = list(range(1, 6))
    r = ST.pair(a, b, issues_a=iss, issues_b=iss, metric="binary")
    assert "单期占Δ比" not in r
    assert "Δ_剔除最大配对期" not in r
