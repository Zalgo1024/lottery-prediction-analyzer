# -*- coding: utf-8 -*-
"""tests/test_profit_backtest.py —— 回测的契约。

最重要的一条：返回 dict 必须与低拥挤四臂回测的 `run_arm` **同构**，
否则 `paired_report` / `sign_test` 无法直接复用，四臂对照就断在这里。
"""
import pytest

from ev.profit import backtest as BT

# 与 run_arm 对齐的键（少一个就没法逐期配对）
RUN_ARM_KEYS = ("期数", "K", "总投注注数", "≥5元期占比", "≥10元期占比",
                "中奖次数", "单期平均中奖注数", "总实得", "净收益", "单期平均净收益",
                "序列")


@pytest.fixture(scope="module")
def arm():
    return BT.run_profit_arm("双色球", K=5, periods=60, seed=0, kappa=0.0,
                             block=30, min_train=1200)


def test_backtest_is_isomorphic_to_run_arm(arm):
    assert arm["期数"] > 0, arm.get("降级")
    for k in RUN_ARM_KEYS:
        assert k in arm, f"缺 run_arm 同构键: {k}"
    assert set(arm["序列"]) == {"≥10元", "中奖注数", "实得"}


def test_series_length_matches_periods(arm):
    n = arm["期数"]
    for k, v in arm["序列"].items():
        assert len(v) == n, f"序列 {k} 长度 {len(v)} ≠ 期数 {n}"


def test_backtest_feeds_paired_report(arm):
    """★ 能被低拥挤回测的配对工具直接消费（这是同构的真正判据）。"""
    from ev.lowcrowd_backtest import paired_report

    rep = paired_report(arm, arm, metric="中奖注数", block=10, reps=200, seed=0)
    assert isinstance(rep, dict) and rep, rep
    assert rep["配对期数"] == arm["期数"], "配对期数应与回测期数一致"
    # 自己与自己配对 ⇒ 两臂均值必然相等、差值序列全 0
    assert rep["A均值"] == rep["B均值"]
    assert rep["块bootstrap"]["差均值"] == pytest.approx(0.0, abs=1e-12)
    assert rep["块bootstrap"]["含零"] is True


def test_tax_is_its_own_column(arm):
    """税必须独立成列，且 税前 − 税 − 成本 == 净收益。"""
    assert "税" in arm and "税前奖金" in arm and "成本" in arm
    assert arm["税前奖金"] - arm["税"] - arm["成本"] == pytest.approx(arm["净收益"], abs=0.02)
    assert arm["税"] >= 0
    assert arm["成本"] == pytest.approx(arm["总投注注数"] * 2.0)


def test_required_metrics_present(arm):
    for k in ("净EV单注", "净盈利期占比", "最大回撤", "最大回撤持续期数",
              "单期集中度", "单次大奖占总实得比"):
        assert k in arm, f"缺方案要求的指标: {k}"
    assert 0.0 <= arm["净盈利期占比"] <= 1.0


def test_deterministic_with_same_seed():
    a = BT.run_profit_arm("双色球", K=3, periods=40, seed=7, block=40, min_train=1200)
    b = BT.run_profit_arm("双色球", K=3, periods=40, seed=7, block=40, min_train=1200)
    assert a["净收益"] == b["净收益"]
    assert a["序列"] == b["序列"]


def test_replay_start_respects_min_train(arm):
    """★ 回放起点不得早于最小训练期数（早于它的期没有可分位的历史）。"""
    assert arm["回放起点"] >= 1200


def test_degradation_when_too_short():
    """窗口短于门槛时必须显式降级，不许硬凑出结论。"""
    out = BT.run_profit_arm("双色球", K=3, periods=10, min_train=10 ** 7)
    assert out["期数"] == 0 and "降级" in out


@pytest.mark.parametrize("nets,expect_mdd,expect_len", [
    ([1, 2, 3], 0.0, 0),
    ([10, -5, -10], 15.0, 2),
    ([-1, -2, -3], 6.0, 3),
    ([5, -1, 10, -20], 20.0, 1),
])
def test_max_drawdown(nets, expect_mdd, expect_len):
    mdd, ln = BT._max_drawdown(nets)
    assert mdd == pytest.approx(expect_mdd)
    assert ln == expect_len
