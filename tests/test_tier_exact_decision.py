# -*- coding: utf-8 -*-
"""决策规则单测：**阈值是纪律，不是参数**。

这一批测试的唯一目的，是把「跑完数据之后能不能改阈值」这件事钉住：
阈值一旦被改，`test_thresholds_are_pinned` 立刻红，逼人先改判据版本并说明理由。
"""

from __future__ import annotations

from ev import tier_exact_decision as DEC


def _rows(*triples):
    return [{"K": k, "Δ_pp": d, "z": z} for k, d, z in triples]


def test_thresholds_are_pinned():
    """阈值写死在这里：改了必须同步改判据版本 + 提交说明，否则这条会红。"""
    assert DEC.THRESH_C_PP == 0.50
    assert DEC.THRESH_A_PP == 1.50
    assert DEC.THRESH_Z == 2.0
    assert DEC.FALLBACK_TOL_PP == 0.05
    assert DEC.HIGH_K_RATIO == 0.70
    assert DEC.判据版本 == "2026-11-03-A"


def test_high_k_is_seventy_percent_of_pool():
    assert DEC.high_k(500) == 350
    assert DEC.high_k(400) == 280


def test_verdict_c_when_all_gains_tiny():
    v = DEC.verdict(_rows((50, 0.05, 1.0), (200, 0.30, 2.0), (354, 0.45, 2.5)))
    assert v["判决"].startswith("C")


def test_verdict_b_when_high_k_falls_back():
    """中后段冲到 1.8pp、高档掉回 1.0pp ⇒ 判「瓶颈在约束」而不是「值得接」。"""
    v = DEC.verdict(_rows((50, 0.1, 1.0), (200, 1.8, 4.0), (354, 1.0, 3.0)))
    assert v["判决"].startswith("B")
    assert v["峰值K"] == 200


def test_fallback_within_tolerance_is_not_b():
    """回落在 0.05pp 容差内不算"拐点"，别把噪声当结构。"""
    v = DEC.verdict(_rows((200, 1.80, 4.0), (354, 1.78, 3.9)))
    assert not v["判决"].startswith("B")


def test_verdict_a_requires_both_thresholds():
    ok = DEC.verdict(_rows((200, 1.0, 3.0), (354, 1.6, 2.5)))
    assert ok["判决"].startswith("A")
    # Δ 够但 z 不够 ⇒ 不许判 A
    weak = DEC.verdict(_rows((200, 1.0, 3.0), (354, 1.6, 1.2)))
    assert weak["判决"] == "边缘"
    # z 够但 Δ 不够 ⇒ 也不许判 A
    small = DEC.verdict(_rows((200, 1.0, 3.0), (354, 1.2, 9.0)))
    assert small["判决"] == "边缘"


def test_verdict_empty_is_not_silent_pass():
    v = DEC.verdict([])
    assert v["判决"] == "无数据"


def test_pool_ratio_flags_degeneracy():
    assert "退化" in DEC.describe_pool_ratio(500, 500)
    assert "自由度很低" in DEC.describe_pool_ratio(500, 450)
    assert "退化" not in DEC.describe_pool_ratio(500, 200)
