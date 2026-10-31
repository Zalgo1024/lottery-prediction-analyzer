# -*- coding: utf-8 -*-
"""tests/test_conservative_ev.py —— 保守情景净 EV 与「要不要考虑倍投」的判决。

任务 4 的三条要求必须都能在代码里被查出来：
① **全单倍**：模块不接受倍数向量（签名里没有 multipliers / weights）；
② **分摊情景**：复用的是 `delta_scenario.evaluate`（δ 只作用一等，主口径）；
③ **门槛**：判决只认最保守那一格（δ=0、奖池 −30%、税后），别的格为正不算通过。

★ 判决几乎必然是「不参与」，这条测试的价值不是复述结论，而是锁住：
  将来有人想让判决通过，就必须动这些断言 —— 那时他必须同时回答「凭什么」。
"""
from __future__ import annotations

import inspect

import pytest

from ev.mechanism.narrative import EV_NEGATIVE_CAVEAT
from ev.profit import conservative_ev as CE
from ev.profit import delta_scenario as DS
from ev.profit.payouts import build_ctx
from ev.tier_backtest import load_test_recs

LOTTERIES = ["双色球", "大乐透"]


def _issue(lot):
    return load_test_recs(lot)[-1].期号


# ---------------------------------------------------------------- ①② 形态
def test_module_takes_no_multiplier_argument():
    """倍投不产生新结局 ⇒ 本模块刻意不提供倍数入口。"""
    for fn in (CE.verdict, CE.grid, CE.conservative_scenario):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"multipliers", "weights", "multiplier", "倍数"}), fn.__name__


@pytest.mark.parametrize("lot", LOTTERIES)
def test_grid_shape_and_range(lot):
    g = CE.grid(lot, _issue(lot))
    assert len(g["格"]) == len(DS.DELTA_GRID) * len(DS.POOL_ERROR_GRID)
    assert g["格数"] if "格数" in g else True
    # 最好的格必须是网格里的真实一格（不是外推出来的）
    best = g["最好的格"]
    assert any(abs(c["净EV_元每注"] - best["净EV_元每注"]) < 1e-12 for c in g["格"])


@pytest.mark.parametrize("lot", LOTTERIES)
def test_pool_error_is_monotone(lot):
    """同一 δ 下，奖池误差越大净 EV 不会更差（池子是钱的来源，不是罚项）。"""
    g = CE.grid(lot, _issue(lot))
    for d in DS.DELTA_GRID:
        rows = sorted([c for c in g["格"] if c["δ"] == d],
                      key=lambda c: c["奖池误差"])
        evs = [c["净EV_元每注"] for c in rows]
        for a, b in zip(evs, evs[1:]):
            assert b >= a - 1e-9, (lot, d, evs)


def test_delta_acts_only_on_top_prize():
    """主口径：δ 只作用一等 ⇒ 二等奖及以下的贡献对 δ 不敏感。

    这是 v1.7 那个「唯一正情景 +6.31 元/100元」的来源被否掉的根因：
    当时把 δ 乘到了所有浮动奖上，凭空造出正期望。
    """
    ctx = build_ctx("双色球", _issue("双色球"))
    low = DS.evaluate(ctx, 0.0)
    high = DS.evaluate(ctx, 1.0)
    for g, row in low["分奖级"].items():
        if g == "一等":
            continue
        assert row["贡献税后_元每注"] == pytest.approx(
            high["分奖级"][g]["贡献税后_元每注"], abs=1e-12), g


# ---------------------------------------------------------------- ③ 判决门槛
@pytest.mark.parametrize("lot", LOTTERIES)
def test_verdict_is_not_to_participate(lot):
    v = CE.verdict(lot, _issue(lot))
    assert v["判决"] == "不参与"
    assert v["保守净EV_元每注"] < 0
    assert v["结构性上限能否补缺口"] is False
    assert "全单倍" in v["倍投口径"]


@pytest.mark.parametrize("lot", LOTTERIES)
def test_conservative_cell_is_the_worst_named_one(lot):
    v = CE.verdict(lot, _issue(lot))
    con = v["保守情景"]
    assert con["δ"] == CE.CONSERVATIVE_DELTA == 0.0
    assert con["奖池误差"] == CE.CONSERVATIVE_POOL_ERROR == -0.30
    assert con["口径"].startswith("主(δ只作用一等)")
    assert con["是否正期望"] is False


def test_positive_scenarios_do_not_pass_the_gate():
    """即使网格里出现正情景，判决也不能因此变成「参与」——门槛只看保守格。"""
    v = CE.verdict("双色球", _issue("双色球"))
    if v["正情景数"] > 0:
        # 若有正情景，必须仍然因为「补不上缺口」或「保守格为负」而不参与
        assert v["判决"] == "不参与"
        assert (v["保守净EV_元每注"] <= 0) or (v["结构性上限能否补缺口"] is False)
    else:
        assert v["正情景数"] == 0


# ---------------------------------------------------------------- 诚实边界
def test_honest_note_reuses_narrative_constant():
    assert EV_NEGATIVE_CAVEAT in CE.HONEST_NOTE
    assert "假设情景变量" in CE.HONEST_NOTE


def test_verdict_carries_rule_version_and_warnings():
    v = CE.verdict("大乐透", _issue("大乐透"))
    g = CE.grid("大乐透", _issue("大乐透"))
    assert g["规则版本"].startswith("dlt-")
    assert isinstance(g["警告"], list)
    assert "诚实边界" in v and "诚实边界" in g
