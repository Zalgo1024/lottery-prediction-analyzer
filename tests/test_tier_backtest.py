# -*- coding: utf-8 -*-
"""tests/test_tier_backtest.py —— 达标回测的契约。

最重要的一条：返回 dict 必须与 `ev.lowcrowd_backtest.run_arm` / `run_profit_arm`
**同构**（11 个键 + `序列` 三个子键），否则 `paired_report` 直接断掉，
三臂配对就无从谈起。本策略自己的指标（达标）另加键，不污染那 11 个。
"""
import pytest

from ev.tier_backtest import (ARMS, TERMINOLOGY_NOTE, compare_tier,
                              run_tier_arm)

# 与 run_arm / run_profit_arm 对齐的键（tests/test_profit_backtest.py 同一份清单）
RUN_ARM_KEYS = ("期数", "K", "总投注注数", "≥5元期占比", "≥10元期占比",
                "中奖次数", "单期平均中奖注数", "总实得", "净收益", "单期平均净收益",
                "序列")

LOT, K, ISSUE = "双色球", 20, "26120"


@pytest.fixture(scope="module")
def arm():
    return run_tier_arm(LOT, "tier_coverage", K, target="四等", periods=30,
                        seed=0, pool_size=300, reselect_every=15)


def test_is_isomorphic_to_run_arm(arm):
    assert arm["期数"] > 0, arm.get("降级")
    for k in RUN_ARM_KEYS:
        assert k in arm, f"缺 run_arm 同构键: {k}"
    assert set(arm["序列"]) == {"≥10元", "中奖注数", "实得"}


def test_series_length_matches_periods(arm):
    n = arm["期数"]
    for k, v in arm["序列"].items():
        assert len(v) == n, f"序列 {k} 长度 {len(v)} ≠ 期数 {n}"
    assert len(arm["序列_达标"]) == n


def test_tier_keys_are_additive_not_replacing(arm):
    """本策略的键是**追加**的：11 个同构键一个不少，且不塞进 `序列`。"""
    assert "达标期占比" in arm and "达标期数" in arm
    assert "达标" not in arm["序列"], "达标序列必须独立成 `序列_达标`，否则破坏同构"
    assert 0.0 <= arm["达标期占比"] <= 1.0


def test_cost_consistency(arm):
    """成本按**实际投注注数**累加：净收益 = 总实得 − 总投注注数×2。"""
    assert arm["净收益"] == pytest.approx(arm["总实得"] - arm["总投注注数"] * 2.0, abs=0.02)
    assert arm["成本"] == pytest.approx(arm["总投注注数"] * 2.0, abs=0.02)


def test_feeds_paired_report(arm):
    """★ 能被低拥挤回测的配对工具直接消费（同构的真正判据）。"""
    from ev.lowcrowd_backtest import paired_report

    rep = paired_report(arm, arm, metric="≥10元", block=5, reps=200, seed=0)
    assert rep["配对期数"] == arm["期数"]
    assert rep["块bootstrap"]["差均值"] == pytest.approx(0.0, abs=1e-12)
    assert rep["块bootstrap"]["含零"] is True


def test_all_arms_return_exactly_k(arm):
    for a in ARMS:
        r = run_tier_arm(LOT, a, K, target="四等", periods=20, seed=0,
                         pool_size=300, reselect_every=20)
        assert r["期数"] == 20, (a, r.get("降级"))
        assert r["K"] == K
        assert r["总投注注数"] == 20 * K, f"{a} 出号数量与 K 不一致 ⇒ 成本口径被污染"


def test_saturation_guard_fires():
    """K 大到 ≥5 元恒中时（双色球六等只需中蓝球，K≥50 必满）必须标 饱和。"""
    r = run_tier_arm(LOT, "random", 60, target="六等", periods=15, seed=0,
                     pool_size=300, reselect_every=15)
    assert r["≥5元期占比"] >= 0.999
    assert r["饱和"] is True and "饱和说明" in r


def test_dlt_version_aware_target():
    """大乐透目标随期号版本走：2026 段的 ≥四等 与 2019 段不是同一批格。"""
    r26 = run_tier_arm("大乐透", "tier_coverage", 10, target="四等", periods=12,
                       seed=0, pool_size=300, reselect_every=12)
    assert r26["规则版本"] == "dlt-2026-02"
    assert r26["达标奖级"] == ["一等", "三等", "二等", "四等"] or \
        sorted(r26["达标奖级"]) == ["一等", "三等", "二等", "四等"]


def test_net_excluding_top_period_is_not_better(arm):
    """★ 剔除单期最大实得后，净收益**只可能更差**（这是算术恒等，不是结论）。

    这一列存在的唯一理由是拦住一种误读：双色球 K=200/500 期下「三等」期望命中
    仅 0.91 次，实测某臂 2 次、另一臂 0 次，单靠 2×3000 元就能把 `净收益/100元`
    拉开 2.5 pp 以上并翻转臂间排序（文档 §7.5）。
    """
    net = arm["净收益/100元"]
    wo = arm["净收益/100元_剔除最大一期"]
    assert "单期最大实得" in arm
    assert wo <= net + 1e-9, "剔除了正实得后净收益反而变好，说明列算错了"
    # 差额必须精确等于 单期最大实得 / 每100元成本
    per100 = arm["总投注注数"] * 2.0 / 100.0
    assert (net - wo) == pytest.approx(arm["单期最大实得"] / per100, abs=1e-4)


def test_unknown_arm_raises():
    with pytest.raises(ValueError):
        run_tier_arm(LOT, "nope", K, periods=5)


def test_compare_tier_shape():
    cmp = compare_tier(LOT, K, target="四等", periods=20, seed=0, seeds=(0, 1),
                       pool_size=300, reselect_every=20, reps=200)
    if "降级" in cmp:
        pytest.skip(cmp["降级"])
    for sec in ("臂", "跨seed全距", "组间差", "配对(≥10元)", "配对(达标)",
                "哨兵_单期平均中奖注数", "术语铁律", "净收益口径"):
        assert sec in cmp, f"缺 {sec}"
    for a in ARMS:
        assert a in cmp["臂"]
        assert 0.0 <= cmp["臂"][a]["达标期占比"] <= 1.0


def test_terminology_note_forbids_overclaim():
    """措辞铁律写死在结果里：只能写「功效不足」，不许写「已证明无效」。"""
    assert "功效不足以识别理论上限" in TERMINOLOGY_NOTE
    assert "证明" not in TERMINOLOGY_NOTE.replace("不得写成「已证明无效/覆盖无效」", "")
