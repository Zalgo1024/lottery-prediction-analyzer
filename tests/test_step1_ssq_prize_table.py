# -*- coding: utf-8 -*-
"""tests/test_step1_ssq_prize_table.py —— 双色球奖级概率表的单一真源锁。

背景（2026-10-23）：`pipeline/step1_probability.prize_probabilities_ssq` 原来是手写
comb()，已与官方规则表漂移：

  · 六等奖只算「0红+1蓝」，漏了 **1红+1蓝 / 2红+1蓝** ⇒ 组合数少算约 2/3；
  · 凭空多出一个 **福运奖（3红+0蓝）**，而双色球规则表没有该奖级 ⇒ 3红+0蓝 是未中，
    把它算成中奖会虚增总中奖概率约 1.2 个百分点。

现在该函数由 `ev/profit/rules.rules_for("双色球").grade_table` 反查生成，与判级函数
`_classify_prize` 同源。本文件把「不再漂移」钉死。
"""
import math

import pytest

from pipeline.step1_probability import prize_probabilities_ssq

TOTAL = math.comb(33, 6) * 16          # 17,721,088


def _cells(h: int, j: int) -> int:
    """(红球命中 h, 蓝球命中 j) 格的中奖组合数（蓝球 16 选 1，命中/不命中各 1/15 种）。"""
    blue = 1 if j == 1 else 15
    return math.comb(6, h) * math.comb(27, 6 - h) * blue


@pytest.fixture(scope="module")
def d():
    return prize_probabilities_ssq()


@pytest.fixture(scope="module")
def lv(d):
    return {x["prize_name"]: x for x in d["levels"]}


def test_total_combinations(d):
    assert d["total_combinations"] == TOTAL == 17_721_088


def test_level_order_and_count(d):
    """六大奖级，按级数升序；不再有第七个「福运奖」。"""
    assert [x["prize_name"] for x in d["levels"]] == \
        ["一等", "二等", "三等", "四等", "五等", "六等"]


def test_no_fuyun_prize(d):
    """3红+0蓝 在双色球规则里是**未中**，不得出现任何「福运」类奖级。"""
    assert not any("福运" in x["prize_name"] for x in d["levels"])
    assert not any("3+0" in x["condition"] for x in d["levels"])


def test_sixth_prize_includes_1red_and_2red(lv):
    """★ 回归核心：六等 = 0红+1蓝 / 1红+1蓝 / 2红+1蓝（旧实现只算了 0红+1蓝）。"""
    assert lv["六等"]["win_combs"] == _cells(0, 1) + _cells(1, 1) + _cells(2, 1)
    assert lv["六等"]["win_combs"] == 1_043_640       # 296010 + 484380 + 263250
    assert "2+1" in lv["六等"]["condition"] and "1+1" in lv["六等"]["condition"]
    assert lv["六等"]["fixed_amount"] == 5


def test_other_levels_win_combs(lv):
    assert lv["一等"]["win_combs"] == _cells(6, 1) == 1
    assert lv["二等"]["win_combs"] == _cells(6, 0) == 15
    assert lv["三等"]["win_combs"] == _cells(5, 1) == 162
    assert lv["四等"]["win_combs"] == _cells(5, 0) + _cells(4, 1) == 7_695
    assert lv["五等"]["win_combs"] == _cells(4, 0) + _cells(3, 1) == 137_475
    assert lv["一等"]["is_floating"] and lv["二等"]["is_floating"]
    assert lv["三等"]["fixed_amount"] == 3000
    assert lv["四等"]["fixed_amount"] == 200
    assert lv["五等"]["fixed_amount"] == 10


def test_total_win_probability(d):
    """总中奖概率 = 1,188,988 / 17,721,088 ≈ 6.709%（旧实现含福运奖时是 7.44%）。"""
    assert sum(x["win_combs"] for x in d["levels"]) == 1_188_988
    assert d["total_win_probability"] == pytest.approx(1_188_988 / TOTAL, abs=1e-12)
    assert d["total_win_probability"] == pytest.approx(0.067094, abs=1e-5)


def test_matches_grade_table_single_source(d):
    """逐格与 `rules_for("双色球").grade_table` 对账 —— 两者同源，不许再各写一份。"""
    from ev.profit.rules import rules_for

    rules = rules_for("双色球")
    want: dict = {}
    for (h, j), g in rules.grade_table.items():
        want[g] = want.get(g, 0) + _cells(h, j)
    got = {x["prize_name"]: x["win_combs"] for x in d["levels"]}
    assert got == want


def test_matches_classify_prize(d):
    """与判级函数一致：所有中奖格都被某个奖级收下，且格数守恒。"""
    from data.feedback import _classify_prize

    seen = 0
    for h in range(7):
        for j in (0, 1):
            g = _classify_prize(h, j)
            if g == "未中":
                continue
            seen += 1
            assert any(x["prize_name"] == g for x in d["levels"]), f"{h}+{j} -> {g} 不在概率表中"
    assert seen == 10          # 双色球共 10 个中奖格


def test_issue_argument_tolerated(d):
    """双色球单版本：传不传期号都一样（与大乐透签名对称，便于统一调用）。"""
    assert prize_probabilities_ssq("26120")["levels"] == d["levels"]


# ---------------------------------------------------------------- 连带修复：浮动奖估算
def test_floating_estimate_is_single_source_and_name_normalized():
    """★ 浮动奖估算键名归一化：『一等奖』≡『一等』。

    历史上 `step2/step4/step5` 各抄了一份 `{"一等奖": ...}` 的表，而规则层与
    `prize_probabilities_*` 返回的是「一等」⇒ `.get("一等", 0)` 恒为 0，
    一/二等奖在静态 EV、方差、凯利里**静默消失**。现在统一到 `data.prize_table`。
    """
    from data.prize_table import estimate_floating_prize

    assert estimate_floating_prize("双色球", "一等") == 5_000_000
    assert estimate_floating_prize("双色球", "一等奖") == 5_000_000
    assert estimate_floating_prize("双色球", "二等") == 200_000
    assert estimate_floating_prize("大乐透", "一等") > 0
    assert estimate_floating_prize("大乐透", "二等") > 0
    # 固定奖不是浮动奖 ⇒ 0
    assert estimate_floating_prize("双色球", "六等") == 0


@pytest.mark.parametrize("lottery", ["双色球", "大乐透"])
def test_static_ev_includes_floating_prizes(lottery):
    """静态 EV 必须包含一/二等奖贡献（否则返奖率会被系统性地算低一大截）。"""
    from pipeline.step2_expected import expected_value

    ev = expected_value(lottery)
    rows = {_n(r["prize"]): r for r in ev["各奖级期望明细"]}
    for g in ("一等", "二等"):
        assert rows[g]["amount"] > 0, f"{lottery} {g} 的估算金额为 0 —— 键名又对不上了"
        assert rows[g]["expected_value"] > 0
    # 一二等奖贡献应显著大于 0（双色球头奖 EV ≈ 1×5e6/1.77e7 ≈ 0.28 元）
    assert rows["一等"]["expected_value"] > 0.01


def test_step6_floating_ev_uses_rule_layer_names():
    """`_floating_prize_ev` 必须能在规则层奖级名上查到（回归「静默 0」）。"""
    from data.loader import load_lottery
    from pipeline.step6_ev import _floating_prize_ev

    data = load_lottery("大乐透")
    rec = next(r for r in data.records if getattr(r, "一等奖奖金", None))
    assert _floating_prize_ev(rec, "大乐透") > 0


def _n(x: str) -> str:
    from data.prize_table import _norm_grade

    return _norm_grade(x)
