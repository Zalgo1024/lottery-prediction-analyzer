# -*- coding: utf-8 -*-
"""tests/test_tier_exact_metrics.py —— `exact_metrics(min_grade=...)` 的契约。

三条硬要求：
1. `min_grade=None` 与原实现**逐位一致**（只多不加改）；
2. 设了 `min_grade` 只影响「达标」口径的概率/注数，**钱一分不改**
   （`税前期望`/`期望税`/`净EV` 必须完全相同）——否则「提高达标率」会被算成「多赚钱」；
3. 单注（K=1）时 `P_达标` 必须等于解析手算值（双色球四等 4.442729e-4、六等 0.067094）。
"""
import pytest

from ev.profit import evaluate as EV
from ev.profit import payouts as P

SSQ = "26120"


@pytest.fixture(scope="module")
def ctx_ssq():
    return P.build_ctx("双色球", SSQ)


@pytest.fixture(scope="module")
def one_ticket_ssq():
    return [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}]


def test_single_ticket_k1_matches_analytic_any_win(ctx_ssq, one_ticket_ssq):
    """K=1 且不设 min_grade ⇒ P(≥1 中奖) = 单注总中奖概率 0.067094。"""
    m = EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, kappa=0.0, want_dist=False)
    assert m["P至少中一注"] == pytest.approx(0.067094, abs=1e-6)
    assert "达标线" not in m and "中奖注数分布" not in m


@pytest.mark.parametrize("grade,expect", [
    ("一等", 1 / 17_721_088),
    ("二等", 16 / 17_721_088),          # (6,1)=1 + (6,0)=15
    ("三等", 178 / 17_721_088),         # + (5,1)=162
    ("四等", 7_873 / 17_721_088),       # + (5,0)=2430 + (4,1)=5265
    ("五等", 145_348 / 17_721_088),     # + (4,0)=78975 + (3,1)=58500
    ("六等", 1_188_988 / 17_721_088),   # + (0/1/2,1)=296010+484380+263250
])
def test_single_ticket_k1_min_grade_matches_analytic(ctx_ssq, one_ticket_ssq, grade, expect):
    m = EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, kappa=0.0,
                         want_dist=False, min_grade=grade)
    assert m["P_达标"] == pytest.approx(expect, abs=1e-9), (grade, m["P_达标"])
    assert m["达标线"] == grade


def test_min_grade_is_monotone(ctx_ssq, one_ticket_ssq):
    """奖级越松，达标概率越大。"""
    ps = []
    for g in ("一等", "二等", "三等", "四等", "五等", "六等", None):
        ps.append(EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, kappa=0.0,
                                   want_dist=False, min_grade=g)["P至少中一注"])
    assert ps == sorted(ps), ps
    assert ps[-1] == pytest.approx(0.067094, abs=1e-6)


def test_money_columns_are_untouched_by_min_grade(ctx_ssq, one_ticket_ssq):
    """★ 钱一分不能变：达标口径只收窄「中奖」判定，奖金仍按真实奖级结算。"""
    base = EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, kappa=0.0, want_dist=False)
    narrow = EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, kappa=0.0,
                              want_dist=False, min_grade="四等")
    for k in ("税前期望", "期望税", "净EV", "净EV单注", "成本"):
        assert narrow[k] == pytest.approx(base[k], abs=1e-12), k
    assert narrow["P_达标"] < base["P至少中一注"]


def test_output_keys_flagged(ctx_ssq, one_ticket_ssq):
    m = EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, kappa=0.0, min_grade="四等")
    assert "达标注数分布" in m and "中奖注数分布" not in m
    assert m["达标注数期望"] == m["中奖注数期望"]          # 同源同值，只是语义标签不同
    assert m["P_达标"] == m["P至少中一注"]


def test_unknown_grade_raises(ctx_ssq, one_ticket_ssq):
    with pytest.raises(ValueError):
        EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, min_grade="十等")
    with pytest.raises(ValueError):
        EV.exact_metrics("双色球", one_ticket_ssq, ctx_ssq, min_grade="直选")


def test_dlt_min_grade_follows_ctx_version():
    """大乐透达标线随**上下文规则版本**走：2026-02 的四等比 2019 的四等宽。"""
    t = [{"红球": [1, 2, 3, 4, 5], "蓝球": [1, 2]}]
    c26 = P.build_ctx("大乐透", "26120")
    c19 = P.build_ctx("大乐透", "23001")
    p26 = EV.exact_metrics("大乐透", t, c26, kappa=0.0, want_dist=False,
                           min_grade="四等")["P_达标"]
    p19 = EV.exact_metrics("大乐透", t, c19, kappa=0.0, want_dist=False,
                           min_grade="四等")["P_达标"]
    assert p26 == pytest.approx(3_216 / 21_425_712, rel=1e-9)
    assert p19 == pytest.approx(216 / 21_425_712, rel=1e-9)   # (4,2)+(5,0)+(5,1)+(5,2)
    assert p26 > p19


def test_multi_ticket_p_any_is_between_single_and_linear_bound(ctx_ssq):
    """K 注时 P(≥1) ≤ K·p（并集上界），且 ≥ 单注 p —— 覆盖思路的立论基础。"""
    p1 = 4.442729e-4
    tickets = [{"红球": sorted([1 + 3 * i, 2 + 3 * i, 3 + 3 * i, 4 + 3 * i, 5 + 3 * i, 6 + 3 * i]),
                "蓝球": [1 + i]} for i in range(10)]
    m = EV.exact_metrics("双色球", tickets, ctx_ssq, kappa=0.0, want_dist=False,
                         min_grade="四等")
    assert p1 <= m["P_达标"] <= 10 * p1 * (1 + 1e-12)
