# -*- coding: utf-8 -*-
"""tests/test_digit_group_prob.py —— 数字型组选3 / 组选6 概率层的契约与牙齿测试。

锁的东西：
  ① 组合计数（形态 10/270/720 分完 1000；票面空间 1000/90/120；覆盖 1/3/6）；
  ② 概率 1/1000、3/1000、6/1000 与「豹子没有组选票」；
  ③ 缓存键按玩法隔离（串键 = 不会报错的错数）；
  ④ 不传 play 时**零改动**（仍是直选），排列5 不受影响；
  ⑤ 组选规则对象拒绝位枚举（model=digit_group）；
  ⑥ EV 逐玩法可复核，返奖率同一水位、判决不变。
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from ev import payout
from ev.profit import digit_group as DG
from ev.profit import rules as R
from data.prize_table import FIXED_PAYOUT

FORBIDDEN = ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死")


# ---------------------------------------------------------------- ① 组合几何
def test_shape_partition_covers_all_ordered_outcomes():
    """分母 = 全部 1000 个有序结局，豹子不许被静默筛掉。"""
    d = DG.shape_distribution()
    assert sum(DG.SHAPE_COUNTS.values()) == DG.OUTCOME_SPACE == 1000
    assert dict(DG.SHAPE_COUNTS) == {"豹子": 10, "对子": 270, "全异": 720}
    assert sum(v["概率"] for v in d.values()) == pytest.approx(1.0)
    assert d["豹子"]["占比"] == Fraction(10, 1000)
    assert d["对子"]["占比"] == Fraction(270, 1000)
    assert d["全异"]["占比"] == Fraction(720, 1000)


@pytest.mark.parametrize("play,space,cover,shape", [
    ("直选", 1000, 1, "全部"),
    ("组选3", 90, 3, "对子"),
    ("组选6", 120, 6, "全异"),
])
def test_play_geometry_exact(play, space, cover, shape):
    g = DG.play_geometry(play)
    assert g["票面空间"] == space
    assert g["每票覆盖结局数"] == cover
    assert g["形态"] == shape
    assert g["中奖概率"] == Fraction(cover, 1000)
    assert g["覆盖结局总数"] == space * cover
    # 返回副本：调用方改动不污染常量
    g["票面空间"] = -1
    assert DG.play_geometry(play)["票面空间"] == space


def test_geometry_self_check_all_pass_and_group_coverage_matches_shapes():
    v = DG.validate_geometry()
    assert v["全部成立"] is True and all(v["逐条"].values())
    assert DG.play_geometry("组选3")["覆盖结局总数"] == DG.SHAPE_COUNTS["对子"]
    assert DG.play_geometry("组选6")["覆盖结局总数"] == DG.SHAPE_COUNTS["全异"]


def test_no_group_ticket_covers_triple_shape():
    """豹子（aaa）只有直选票能中：组选3 要求恰好一对、组选6 要求三个互异。"""
    assert DG.play_geometry("组选3")["形态"] == "对子"
    assert DG.play_geometry("组选6")["形态"] == "全异"
    covered = (DG.play_geometry("组选3")["覆盖结局总数"]
               + DG.play_geometry("组选6")["覆盖结局总数"])
    assert covered == 270 + 720
    assert DG.OUTCOME_SPACE - covered == DG.SHAPE_COUNTS["豹子"] == 10
    assert any("豹子" in n for n in (DG.NOTE_SHAPE,))


# ---------------------------------------------------------------- ② 概率层
@pytest.mark.parametrize("play,p", [
    ("直选", 1 / 1000), ("组选3", 3 / 1000), ("组选6", 6 / 1000),
])
def test_win_probability_and_grade_probabilities(play, p):
    assert DG.win_probability(play) == pytest.approx(p)
    assert DG.grade_probabilities(play) == {play: p}
    # 不写「未中」：下游逐奖级查奖金表时不会被静默当成 0 元档
    assert "未中" not in DG.grade_probabilities(play)


@pytest.mark.parametrize("lot", DG.DIGIT_GROUP_LOTTERIES)
def test_payout_grade_probabilities_default_unchanged(lot):
    """不传 play ⇒ 仍是直选，既有调用点零改动。"""
    assert payout.grade_probabilities(lot) == {"直选": 1 / 1000}
    assert set(R.rules_for(lot).grade_table) == {(3, 0)}
    assert {"组选3", "组选6"} <= set(R.rules_for(lot).grades)


@pytest.mark.parametrize("lot", DG.DIGIT_GROUP_LOTTERIES)
@pytest.mark.parametrize("play,p", [("组选3", 3 / 1000), ("组选6", 6 / 1000)])
def test_payout_grade_probabilities_group_plays(lot, play, p):
    got = payout.grade_probabilities(lot, play=play)
    assert set(got) == {play} and got[play] == pytest.approx(p)
    # 玩法名必须能在奖金表里查到金额，否则 EV 链会拿 0 元当奖金
    assert play in FIXED_PAYOUT[lot]


def test_prob_cache_keys_are_separated_per_play():
    """★ 串键事故：三种玩法共用 ("排列3","default") ⇒ 先算的喂给后两个。"""
    a = payout.grade_probabilities("排列3")
    b = payout.grade_probabilities("排列3", play="组选3")
    c = payout.grade_probabilities("排列3", play="组选6")
    assert a == {"直选": 1 / 1000} and b == {"组选3": 3 / 1000}
    assert c == {"组选6": 6 / 1000}
    assert payout.grade_probabilities("排列3") == a      # 反序复核
    assert payout._default_version("排列3", "组选3") == "default:组选3"
    assert payout._default_version("排列3") == "default"
    assert set(payout._PROB_CACHE) >= {
        ("排列3", "default"), ("排列3", "default:组选3"),
        ("排列3", "default:组选6")}


def test_explicit_version_with_play_still_uses_group_model():
    assert payout.grade_probabilities(
        "排列3", version="default", play="组选3") == {"组选3": 3 / 1000}
    assert payout._grade_probabilities_impl(
        "排列3", "default:组选6", None) == {"组选6": 6 / 1000}


def test_unknown_play_is_rejected_not_silently_defaulted():
    for fn in (DG.play_geometry, DG.win_probability, DG.grade_probabilities):
        with pytest.raises(KeyError, match="玩法"):
            fn("组选9")
    with pytest.raises(KeyError, match="没有组选玩法"):
        R.digit_play_rules("排列5", "组选3")
    with pytest.raises(KeyError, match="没有组选玩法"):
        DG.ev_table("排列5")


def test_pl5_has_no_group_layer():
    assert payout.grade_probabilities("排列5") == {"直选": 1 / 100000}
    assert "排列5" not in DG.DIGIT_GROUP_LOTTERIES


# ---------------------------------------------------------------- ③ 规则对象
@pytest.mark.parametrize("lot", DG.DIGIT_GROUP_LOTTERIES)
@pytest.mark.parametrize("play,amt", [("组选3", 346.0), ("组选6", 173.0)])
def test_group_rules_refuse_position_enumeration(lot, play, amt):
    r = R.rules_for(lot, play=play)
    assert r.model == "digit_group" and r.play == play
    assert r.ticket_space == (90 if play == "组选3" else 120)
    assert r.grade_table == {(3, 0): play}
    assert r.grades[play].fixed == amt
    with pytest.raises(NotImplementedError, match="digit_group"):
        _ = r.outcome_size


def test_rules_for_direct_play_alias_matches_default():
    for lot in DG.DIGIT_GROUP_LOTTERIES:
        a = R.rules_for(lot)
        b = R.rules_for(lot, play="直选")
        assert a.grade_table == b.grade_table == {(3, 0): "直选"}
        assert b.model == "binary_cell" and b.ticket_space is None


def test_group_plays_do_not_pollute_grade_rank_names():
    names = R.grade_rank_names()
    assert "直选" not in names and "组选3" not in names and "组选6" not in names
    assert names[0] == "一等"


# ---------------------------------------------------------------- ④ EV
@pytest.mark.parametrize("lot", DG.DIGIT_GROUP_LOTTERIES)
def test_ev_table_is_reproducible_and_verdict_unchanged(lot):
    t = DG.ev_table(lot)
    assert t["不接生产"] is True and t["彩种"] == lot
    assert t["几何自检"]["全部成立"] is True
    assert t["奖金来源"] == "data.prize_table.FIXED_PAYOUT"
    rows = {r["玩法"]: r for r in t["玩法"]}
    assert set(rows) == set(DG.PLAYS)
    for play, r in rows.items():
        assert r["奖金元"] == FIXED_PAYOUT[lot][play]
        assert r["期望回款元"] == pytest.approx(
            r["奖金元"] * r["中奖概率"], abs=1e-6)
        assert r["净EV元"] == pytest.approx(r["期望回款元"] - 2.0, abs=1e-6)
        assert r["返奖率"] < 1.0
        assert r["返奖率"] == pytest.approx(0.52, abs=0.002)
    # 换玩法不翻转「无可证明优势（不参与）」的判决
    assert max(r["净EV元"] for r in rows.values()) < 0
    assert any("不构成证明" in n for n in t["口径"])
    assert any("不接生产选号" in n for n in t["口径"])


def test_ev_table_cost_is_injectable():
    t = DG.ev_table("福彩3D", cost=4.0)
    assert all(r["净EV元"] == pytest.approx(r["期望回款元"] - 4.0, abs=1e-6)
               for r in t["玩法"])
    assert all(r["返奖率"] == pytest.approx(r["期望回款元"] / 4.0, abs=1e-6)
               for r in t["玩法"])


def test_module_text_has_no_forbidden_words():
    from pathlib import Path
    text = (Path(DG.__file__).read_text(encoding="utf-8"))
    for bad in FORBIDDEN:
        assert bad not in text, f"禁词 {bad}"
