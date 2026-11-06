# -*- coding: utf-8 -*-
"""七乐彩：规则表 + 精确概率（含「为什么不能用通用式」的反例锁）。

与既有 6 彩种的关键差别：开奖是 **7 个基本号 + 从剩余 23 个摇 1 个特别号**，
票面只有 7 个号。所以 `Zone` 里特别号是 draw_only，概率也必须用双层精确式。
"""
from __future__ import annotations

import math

import pytest

from data.prize_table import FIXED_PAYOUT, REGULATED_NOMINAL
from ev.payout import _probs_from_grade_table, grade_probabilities
from ev.profit import rules as R
from data.schema import get_schema
from data.feedback import _settle_pl
from ev.profit.settle import _grade
from ev.profit.amounts import grade_amount

DEN = math.comb(30, 7)          # 2,035,800
# 各档「分子」（分母固定 DEN）—— 由 C(7,h)·C(1,j)·C(22,7-h-j) 得出
EXACT = {"一等": 1, "二等": 7, "三等": 154, "四等": 462,
         "五等": 4_851, "六等": 8_085, "七等": 53_900}


# ---------------------------------------------------------------- 规则表
def test_qlc_grade_table():
    r = R.rules_for("七乐彩")
    assert r.grade_table == {
        (7, 0): "一等", (6, 1): "二等", (6, 0): "三等",
        (5, 1): "四等", (5, 0): "五等", (4, 1): "六等", (4, 0): "七等",
    }
    assert r.outer_width == 7 and r.outer_range == (1, 30)
    assert r.inner_size == 23          # 特别号来自「剩余 23 个」，不是 30
    assert r.model == "binary_cell"


def test_qlc_seven_is_impossible_with_special():
    """中 7 个基本号时票里已没有第 8 个号，不可能再含特别号 ⇒ (7,1) 不入表。"""
    assert (7, 1) not in R.rules_for("七乐彩").grade_table


def test_qlc_fixed_and_regulated_split():
    g = R.rules_for("七乐彩").grades
    for name in ("四等", "五等", "六等", "七等"):
        assert g[name].kind == "fixed", name
    for name in ("一等", "二等", "三等"):
        assert g[name].kind == "regulated", name
    assert g["四等"].fixed == 200 and g["七等"].fixed == 5
    # 三等实测最小 400 ⇒ 限赔保底
    assert g["三等"].floor == 400
    # 一等奖没有 500 万封顶（实测最大 847 万）
    assert g["一等"].cap is None


def test_qlc_tables_present():
    assert FIXED_PAYOUT["七乐彩"] == {"四等": 200, "五等": 50, "六等": 10, "七等": 5}
    assert set(REGULATED_NOMINAL["七乐彩"]) == {"一等", "二等", "三等"}


# ---------------------------------------------------------------- 精确概率
@pytest.mark.parametrize("grade,mol", sorted(EXACT.items()))
def test_qlc_exact_numerator(grade, mol):
    p = grade_probabilities("七乐彩")
    assert round(p[grade] * DEN) == mol, f"{grade} 概率分子不对"


def test_qlc_total_hit_rate():
    p = grade_probabilities("七乐彩")
    assert sum(EXACT.values()) == 67_460
    assert sum(p.values()) == pytest.approx(67_460 / DEN)


def test_qlc_outcome_size():
    assert R.rules_for("七乐彩").outcome_size == DEN * 23


# ---------------------------------------------------------------- ★ 反例锁
def test_generic_formula_would_misallocate_grades():
    """通用式 `_probs_from_grade_table` 对七乐彩会**档位错配**。

    它假设「开奖也是 7 个号」，于是把特别号当成"从 23 个里随机取 1 个份额"，
    实际特别号是确定的那一个。实测后果：

        四等 462 → 231   (−50%)
        六等 8085 → 2695 (−66.7%)
        七等 53900 → 59290 (+10%)
        一等 1 → 0.96    (−4.3%)

    ★★ 最危险的地方：两边**分子合计完全相同**（67,460），所以
      总中奖率与「≥10元」达标率**都对得上**，聚合指标查不出错误。
      这就是本测试存在的理由 —— 防止有人"为了统一"把七乐彩也走通用式。
    """
    gen = _probs_from_grade_table("七乐彩", "qlc-2004")
    right = grade_probabilities("七乐彩")

    assert round(gen["四等"] * DEN) == 231
    assert round(gen["六等"] * DEN) == 2_695
    assert round(right["四等"] * DEN) == 462
    assert round(right["六等"] * DEN) == 8_085

    # 两边总中奖率一致 —— 正是它给了"看起来没问题"的假象。
    # 注意只能近似比：通用式里 (7,0) 拿到的是 22/23 的份额（0.9565 而非 1），
    # 差的 4.3% 由七等多出的 10% 抵回来，净差 ~6e-7 相对。
    assert sum(gen.values()) == pytest.approx(sum(right.values()), rel=1e-5)
    assert abs(sum(gen.values()) - sum(right.values())) < 1e-7


def test_qlc_ev_is_lower_if_using_generic_formula():
    """通用式会把名义 EV 低估约 4.8%（1.2008 → 1.1434），方向与档位偏差无关。"""
    r = R.rules_for("七乐彩")
    gen = _probs_from_grade_table("七乐彩", "qlc-2004")
    right = grade_probabilities("七乐彩")

    def ev(p):
        return sum(p[g] * (r.grades[g].fixed or r.grades[g].nominal) for g in p)

    e_right, e_gen = ev(right), ev(gen)
    assert e_right == pytest.approx(1.20, abs=0.01)
    assert e_gen < e_right


# ---------------------------------------------------------------- 判级
@pytest.mark.parametrize("hits,special,grade", [
    (7, 0, "一等"), (6, 1, "二等"), (6, 0, "三等"),
    (5, 1, "四等"), (5, 0, "五等"), (4, 1, "六等"), (4, 0, "七等"),
    (3, 1, None), (3, 0, None), (0, 0, None),
])
def test_grade_of_hits(hits, special, grade):
    assert R.grade_of_hits("七乐彩", hits, special) == grade


def test_grade_of_hits_does_not_take_over_old_lotteries():
    """既有 6 彩种的判级在 data/feedback 的专用函数里 —— 这里不接管，避免回归。"""
    with pytest.raises(KeyError, match="不接管"):
        R.grade_of_hits("双色球", 6, 1)


# ---------------------------------------------------------------- 结算路径
# 合成开奖：基本号 1-7，特别号 30（取自剩余号码，票面不含）。
# 特别号是 draw_only：h = 基本号命中数，j = 1 当且仅当票面含开奖特别号值。
QLC_SCHEMA = get_schema("七乐彩")
QLC_ACTUAL = {"基本号": [1, 2, 3, 4, 5, 6, 7], "特别号": [30]}
QLC_ISSUE = "2026125"  # 库内存在，一~三等取实测单注；四~七等为固定奖

# (票面基本号, 期望奖级, 期望 h, 期望 j)
_QLC_SETTLE = [
    ([1, 2, 3, 4, 5, 6, 7], "一等", 7, 0),    # 7 基本，不含特别号
    ([1, 2, 3, 4, 5, 6, 30], "二等", 6, 1),   # 6 基本 + 含特别号
    ([1, 2, 3, 4, 5, 6, 8], "三等", 6, 0),    # 6 基本 + 不含特别号
    ([1, 2, 3, 4, 5, 30, 8], "四等", 5, 1),
    ([1, 2, 3, 4, 5, 8, 9], "五等", 5, 0),
    ([1, 2, 3, 4, 30, 8, 9], "六等", 4, 1),
    ([1, 2, 3, 4, 8, 9, 10], "七等", 4, 0),
    ([1, 2, 3, 8, 9, 10, 11], None, 3, 0),    # 仅 3 基本 → 未中
]


def _qlc_ticket(basic):
    return {"号码": {"基本号": list(basic), "特别号": []}}


@pytest.mark.parametrize("basic,grade,h,j", _QLC_SETTLE)
def test_qlc_grade_settlement(basic, grade, h, j):
    """_grade 七乐彩：二元格 (h, j) 判级 + 唯一取值链 grade_amount。

    受规则约束奖级（一~三等）的单注金额按期公布，判级层必须以
    grade_amount 为唯一真值链（实测优先、名义兜底）；这里做回归锁，
    展示层 data.prize_table.prize_payout 也复用同一条链。
    """
    g, pay = _grade("七乐彩", _qlc_ticket(basic), QLC_ACTUAL, QLC_SCHEMA, QLC_ISSUE)
    assert g == grade
    if grade is None:
        assert pay == 0.0
    else:
        exp, _src = grade_amount("七乐彩", QLC_ISSUE, grade)
        assert pay == pytest.approx(float(exp) if exp is not None else 0.0)


@pytest.mark.parametrize("basic,grade", [(c[0], c[1]) for c in _QLC_SETTLE])
def test_qlc_settle_pl_feedback(basic, grade):
    """反馈层 _settle_pl 七乐彩：按真实奖级结算，盈亏 = 实得 − 2 元。"""
    fb = {"预测号码": {"基本号": list(basic), "特别号": []},
          "实际号码": QLC_ACTUAL, "期号": QLC_ISSUE}
    pl, mode, level = _settle_pl("七乐彩", fb)
    assert level == grade
    assert mode == "名义EV"
    if grade is None:
        assert pl == pytest.approx(-2.0)
    else:
        exp, _src = grade_amount("七乐彩", QLC_ISSUE, grade)
        exp_pl = round(float(exp) - 2.0, 2) if exp is not None else 0.0
        assert pl == pytest.approx(exp_pl)
