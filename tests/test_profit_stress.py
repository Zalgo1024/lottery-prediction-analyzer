# -*- coding: utf-8 -*-
"""tests/test_profit_stress.py —— 不确定情景下的策略压力测试。

锁三件事：
  ① 扰动算子本身正确（scale=0 ⇒ 无信息；均匀缩放保序；局部错标确实打乱）；
  ② 无浮动奖/无热度模型的彩种必须判「无优势」（不许凭空造出优势）；
  ③ 双色球这类有优势的彩种：噪声越大保留率越低，拥挤度缺失时优势归零。
"""
from __future__ import annotations

import numpy as np
import pytest

from ev.profit import stress as ST
from ev.profit import tilt as T


def _fake_tilt(n: int = 60, seed: int = 0) -> T.Tilt:
    rng = np.random.default_rng(seed)
    s = rng.normal(0.0, 0.5, size=n)
    q = np.argsort(np.argsort(s)) / max(1, n - 1)
    return T.Tilt(lottery="双色球", scores=s, quantiles=q,
                  source="measured_GO", offset=0.3, note="", beta_asof=None)


# ---------------------------------------------------------------- 扰动算子
def test_perturb_scale_zero_means_no_info():
    """scale=0 ⇒ 分数全 0、offset=0（无分奖信息 = 均匀）。"""
    pt = ST._perturb(_fake_tilt(), scale=0.0)
    assert np.allclose(pt.scores, 0.0)
    assert pt.offset == 0.0


def test_perturb_noise_changes_scores_but_stays_centered():
    base = _fake_tilt()
    pt = ST._perturb(base, noise_sigma=0.5, seed=1)
    assert pt.scores.mean() == pytest.approx(0.0, abs=1e-12)   # 中心化
    assert not np.allclose(pt.scores, base.scores)             # 确实被扰动


def test_perturb_scale_preserves_rank_order():
    """均匀缩放只改幅度不改排序 ⇒ 对按排名选票的策略无影响（实测保留率 100%）。"""
    base = _fake_tilt()
    pt = ST._perturb(base, scale=2.0)
    assert np.array_equal(np.argsort(base.scores), np.argsort(pt.scores))


def test_corrupt_fraction_actually_scrambles_some():
    base = _fake_tilt(n=200, seed=0)
    pt = ST._corrupt_fraction(base, fraction=0.2, seed=3)
    changed = int(np.sum(~np.isclose(base.scores, pt.scores)))
    assert changed >= 20        # 约 20% 的位置被换掉


# ---------------------------------------------------------------- 行为
@pytest.mark.realdata
def test_no_advantage_lottery_reports_so():
    """无浮动奖的彩种（排列3）基准优势≈0 ⇒ 判决「无优势」，不许凭空造优势。"""
    r = ST.stress_test("排列3", "2026293", K=10)
    assert r["判决"] == "无优势"
    assert r["基准优势_元每注"] <= ST.ADV_EPS
    assert r["前视检查"] is True


@pytest.mark.realdata
def test_ssq_advantage_decays_under_noise_and_vanishes_without_info():
    """双色球：基准有正优势，但噪声越大保留率越低；拥挤度缺失时优势归零。"""
    r = ST.stress_test("双色球", "26126", K=20)
    assert r["前视检查"] is True
    assert r["基准优势_元每注"] > ST.ADV_EPS
    scen = {s["情景"]: s for s in r["情景"]}

    # 噪声单调衰减（σ 越大保留越少）
    assert scen["热度噪声σ=0.1"]["保留率"] > scen["热度噪声σ=1"]["保留率"]
    # 均匀缩放保序 ⇒ 100%（这是真结果，不是 bug）
    assert scen["热度偏差δ=+0.5"]["保留率"] == pytest.approx(1.0, abs=1e-9)
    # 局部错标确实伤优势
    assert scen["局部错标20%"]["保留率"] < 1.0
    # 拥挤度缺失 ⇒ 优势按定义为 0、不再优于随机
    miss = scen["拥挤度缺失(无分奖信息)"]
    assert miss["优势_元每注"] == 0.0
    assert miss["仍优于随机"] is False
    # 因此判决应为「退化」（优势在无信息时消失）
    assert r["判决"] == "退化"

# ---------------------------------------------------------------- 决策流接入
def test_stress_gate_matrix_only_downgrades():
    """鲁棒性闸门：上游可参与时退化/无优势 ⇒ 降为不建议参与；绝不升级。"""
    from ev.profit.solvers import (_stress_gate, VERDICT_PARTICIPATE,
                                   VERDICT_REJECT, VERDICT_NO_DATA)

    for rob in ("退化", "无优势"):
        g = _stress_gate(VERDICT_PARTICIPATE, {"判决": rob, "原因": "x"})
        assert g["是否降级"] is True and g["最终判决"] == VERDICT_REJECT
    for rob in ("脆弱", "稳定"):
        g = _stress_gate(VERDICT_PARTICIPATE, {"判决": rob, "原因": "x"})
        assert g["是否降级"] is False and g["最终判决"] == VERDICT_PARTICIPATE
    for up in (VERDICT_REJECT, VERDICT_NO_DATA):
        for rob in ("退化", "无优势", "脆弱", "稳定"):
            g = _stress_gate(up, {"判决": rob, "原因": "x"})
            assert g["是否降级"] is False and g["最终判决"] == up   # 绝不升级


@pytest.mark.realdata
def test_decide_without_stress_adds_no_key():
    """默认（不传 with_stress）不得新增键 —— 既有调用点与快照不受影响。"""
    from ev.profit.solvers import decide
    d = decide("双色球", "26126", K=10)
    assert "鲁棒性" not in d and "鲁棒性闸门" not in d


@pytest.mark.realdata
def test_decide_with_stress_attaches_evidence_and_never_upgrades():
    """with_stress=True：挂上鲁棒性证据；判决只可能降级、不会升级。"""
    from ev.profit.solvers import decide
    base = decide("双色球", "26126", K=10)
    d = decide("双色球", "26126", K=10, with_stress=True)
    assert "鲁棒性" in d and "鲁棒性闸门" in d
    assert d["鲁棒性"]["判决"] in ("退化", "无优势", "脆弱", "稳定")
    # 只降不升：开启压力测试后，判决不可能比不开时更乐观
    assert d["判决"] in (base["判决"], "本期不建议参与")
