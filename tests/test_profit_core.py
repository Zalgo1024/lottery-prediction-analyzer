# -*- coding: utf-8 -*-
"""tests/test_profit_core.py —— 盈利模型的地基（规则表 / 枚举 / 倾斜）。

★ 本文件里最重要的是 `test_kappa0_histogram_matches_combinatorial`：
  它锁住「命中直方图在 κ=0 时退化成组合精确概率」。
  这条不过，后面所有净EV 数字都不可信（方案里定为 M2 的地基闸门）。
"""
import math

import numpy as np
import pytest

from ev.payout import grade_probabilities
from ev.profit import outcomes as O
from ev.profit import rules as R
from ev.profit import tilt as T


# ---------------------------------------------------------------- 规则表

def _outer_pmf(r):
    n = r.outer_range[1] - r.outer_range[0] + 1
    w = r.outer_width
    if r.outer_choose:
        return {h: math.comb(w, h) * math.comb(n - w, w - h) / math.comb(n, w)
                for h in range(w + 1)}
    return {h: math.comb(w, h) * (n - 1) ** (w - h) / n ** w for h in range(w + 1)}


@pytest.mark.parametrize("name,issue", [
    ("双色球", None),
    ("大乐透", None), ("大乐透", "26013"), ("大乐透", "19018"),
    ("大乐透", "14051"), ("大乐透", "09121"),
    ("七星彩", None), ("排列3", None), ("福彩3D", None), ("排列5", None),
])
def test_rules_reproduce_combinatorial_probabilities(name, issue):
    """规则表（格表 + 内层重数）必须逐奖级复现 payout.grade_probabilities 的同版本值。

    大乐透有五个版本（8/8/6/9/7 级），必须按版本比对，否则会拿九级概率当七级用。
    """
    r = R.rules_for(name, issue)
    pmf = _outer_pmf(r)
    got = {}
    for (h, j), g in r.grade_table.items():
        got[g] = got.get(g, 0.0) + pmf[h] * r.inner_multiplicity[j] / r.inner_size
    want = grade_probabilities(name, version=r.rule_version)
    assert set(got) == set(want), f"奖级集合不一致({name} {issue}): {set(got) ^ set(want)}"
    for g, p in want.items():
        assert got[g] == pytest.approx(p, rel=1e-12), f"{name} {issue} {g}"


def test_dlt_all_versions_share_win_probability():
    """13 个中奖条件跨段守恒 ⇒ 各版本「中任意奖」的总概率必须相同。"""
    from data.prize_versions import DLT_VERSIONS
    totals = [sum(grade_probabilities("大乐透", version=v.version).values())
              for v in DLT_VERSIONS]
    assert max(totals) - min(totals) < 1e-12, totals


@pytest.mark.parametrize("name", [n for n in R.SUPPORTED if n != "快乐8"])
def test_outcome_size_matches_space(name):
    expect = {"双色球": 17_721_088, "大乐透": 21_425_712, "七星彩": 15_000_000,
              "排列5": 100_000, "排列3": 1_000, "福彩3D": 1_000,
              # 七乐彩：C(30,7) × 23（特别号从剩余 23 个摇出）= 46,823,400
              "七乐彩": 46_823_400}[name]
    assert R.rules_for(name).outcome_size == expect


def test_kl8_outcome_size_refuses_to_enumerate():
    """快乐8 选十的「结局数」是 C(80,10)=1.65e12 —— 任何枚举都会挂死。

    宁可显式失败，也不要给一个看着合法的巨数，让下游 lru_cache 去建 16 亿行矩阵。
    """
    with pytest.raises(NotImplementedError, match="hypergeom"):
        R.rules_for("快乐8").outcome_size
    with pytest.raises(NotImplementedError, match="不可枚举"):
        from ev.profit.outcomes import outer_universe
        outer_universe("快乐8")


def test_dlt_ticket_price_reflects_addon():
    """大乐透票量必须按 2.2648 折算（追加投注占 26.5%），否则高估票量 13%。"""
    assert R.rules_for("大乐透").avg_ticket_price == pytest.approx(2.2648, abs=1e-4)
    assert R.rules_for("双色球").avg_ticket_price == pytest.approx(2.0)


# ---------------------------------------------------------------- 枚举内核（地基）

_TICKETS = {
    "双色球": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]},
               {"红球": [7, 8, 9, 10, 11, 12], "蓝球": [16]},
               {"红球": [20, 21, 22, 23, 24, 25], "蓝球": [8]}],
    "大乐透": [{"红球": [1, 2, 3, 4, 5], "蓝球": [1, 2]},
               {"红球": [6, 7, 8, 9, 10], "蓝球": [3, 4]},
               {"红球": [20, 21, 22, 23, 24], "蓝球": [11, 12]}],
}


@pytest.mark.parametrize("name", ["双色球", "大乐透"])
def test_kappa0_histogram_matches_combinatorial(name):
    """★ 地基：κ=0 时由命中直方图推出的各奖级期望注数 == 组合精确概率 × 注数。"""
    r = R.rules_for(name)
    tks = _TICKETS[name]
    H = O.hit_histogram(name, tks)
    assert H.shape == (r.outer_size, r.outer_width + 1)
    assert (H.sum(axis=1) == len(tks)).all(), "每行的命中计数之和必须等于注数"

    ph = H.sum(axis=0) / H.shape[0]          # κ=0 ⇒ 外层权重均匀
    m_inner = O.inner_multiplicity(r) / float(r.inner_size)
    got = {g: 0.0 for g in r.grades}
    for h in range(H.shape[1]):
        for j, mj in enumerate(m_inner):
            g = r.grade_of(h, j)
            if g in got:
                got[g] += ph[h] * mj

    for g, p in grade_probabilities(name).items():
        assert got[g] == pytest.approx(p * len(tks), rel=1e-10, abs=1e-18), f"{name} {g}"


def test_histogram_row_sum_invariant():
    """H 每行和恒等于注数 K —— 与结局无关的强不变量，能抓出编码/分块错误。"""
    tks = _TICKETS["双色球"]
    H = O.hit_histogram("双色球", tks, chunk=50_000)
    assert (H.sum(axis=1) == len(tks)).all()


# ---------------------------------------------------------------- 倾斜

def test_tilt_scores_centered_and_quantiles_unbiased():
    tl = T.outer_tilt("双色球")
    if tl is None:
        pytest.skip("双色球 popularity 模型未 Go（数据不足时跳过）")
    assert abs(float(tl.scores.mean())) < 1e-9, "s(ω) 必须中心化（E_uniform[s]=0）"
    # 中点秩的无偏性：公平开奖下 E[u] 恰为 0.5
    assert float(tl.quantiles.mean()) == pytest.approx(0.5, abs=1e-6)


def test_tilt_kappa_zero_is_exactly_uniform():
    tl = T.outer_tilt("双色球")
    if tl is None:
        pytest.skip("popularity 未 Go")
    w = T.weights(tl, 0.0)
    assert w.sum() == pytest.approx(1.0, rel=1e-12)
    assert np.ptp(w) < 1e-15, "κ=0 必须是严格均匀"
    assert T.normalization(tl, 0.0) == float(len(tl.scores))
    assert T.implied_mean_quantile(tl, 0.0) == pytest.approx(0.5, abs=1e-6)


def test_tilt_monotone_in_kappa():
    """κ 越大，平均分位越冷、最冷号提升越大 —— 这是 κ 的语义定义。"""
    tl = T.outer_tilt("双色球")
    if tl is None:
        pytest.skip("popularity 未 Go")
    us = [T.implied_mean_quantile(tl, k) for k in (0.0, 0.1, 0.3, 0.5, 1.0)]
    assert all(us[i] > us[i + 1] for i in range(len(us) - 1)), us
    boosts = [T.cold_boost(tl, k) for k in (0.0, 0.5, 1.0)]
    assert boosts[0] == pytest.approx(1.0) and boosts[1] > 1.0 and boosts[2] > boosts[1]


def test_unsupported_lottery_returns_none_not_uniform():
    """不支持/未 Go 的彩种必须显式返回 None，不许静默退化成均匀。

    改写自原「福彩3D / 七星彩 都返回 None」：现在数字型/七星彩 走代理 π
    （crowd_pi_v2）来源，所以：
      · 福彩3D 因直选注数自 2022 起全 0 ⇒ π 不可测 ⇒ 仍显式 None；
      · 排列5 走 crowd_pi_v2，返回 Tilt 且分数**非全零**（不得退化成均匀）。
    """
    # 福彩3D：直选注数自 2022 起全 0 ⇒ π 不可测 ⇒ 显式 None（不是退化成均匀）
    assert T.outer_tilt("福彩3D") is None
    # 排列5：代理 π 来源（crowd_pi_v2），返回 Tilt 且不退化成均匀
    tl = T.outer_tilt("排列5")
    assert tl is not None
    assert tl.source == "crowd_pi_v2"
    assert not np.allclose(tl.scores, 0.0)        # 非退化成均匀（分数全 0）
