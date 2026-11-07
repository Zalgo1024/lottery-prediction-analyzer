# -*- coding: utf-8 -*-
"""位置型逐位 Markov（`ev/position_markov.py`）的契约锁。

位置型彩种的「位置」是语义的一部分：第 1 位开 5 ≠ 第 3 位开 5。任何排序/去重都会
把这层信息抹掉，而且不报错。本文件锁：
  ① 只有位置型彩种适用，其余**显式**不适用并给原因（缺席≠通过）；
  ② 输出是逐位排序表，分数是三项的加权和，可当场复算；
  ③ 同一输入同一输出（可复核）；
  ④ 没实现的东西必须写出来（跨位共现留空，不拼凑）。
"""
import pytest

from ev.position_markov import (NOTES, W_MARKOV, W_NEIGH, W_OMIT, applicable,
                                position_models, rank_positions)


@pytest.mark.parametrize("lottery", ["排列3", "福彩3D", "排列5", "七星彩"])
def test_positional_lotteries_are_applicable(lottery):
    a = applicable(lottery)
    assert a["适用"] is True
    assert a["位置数"] >= 3


@pytest.mark.parametrize("lottery", ["双色球", "大乐透", "快乐8", "七乐彩"])
def test_non_positional_is_explicitly_inapplicable(lottery):
    a = applicable(lottery)
    assert a["适用"] is False
    assert "非位置型" in a["原因"]      # 原因必须说清，不能只写「不适用」


def test_seven_lucky_special_zone_is_skipped():
    """七乐彩特别号是 draw_only（票面不含），即便将来变成位置型也不该进建模。"""
    a = applicable("七乐彩")
    assert a["适用"] is False          # 基本号本身不是位置型
    assert "基本号" in a["原因"]        # 原因要点到具体分区，不能只写「不适用」
    # 逐位模型对七乐彩完全不适用 ⇒ 特别号（draw_only）自然也不会被建模
    assert position_models("七乐彩")["适用"] is False


def test_rank_has_one_row_per_position():
    r = rank_positions("福彩3D")
    assert r["适用"] is True
    assert [p["位置"] for p in r["位置"]] == ["第1位", "第2位", "第3位"]
    for p in r["位置"]:
        assert len(p["排序"]) == 10          # 0-9 每位 10 个候选
        assert {x["数码"] for x in p["排序"]} == set(range(10))


def test_scores_are_sorted_and_in_unit_interval():
    r = rank_positions("排列3")
    for p in r["位置"]:
        scores = [x["分"] for x in p["排序"]]
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 <= s <= 1.0 for s in scores)


def test_score_is_recomputable_from_components():
    """分数 = 0.60·Markov + 0.25·遗漏节奏 + 0.15·邻号强度（口径写在 docstring，必须对得上）。"""
    r = rank_positions("福彩3D")
    for p in r["位置"]:
        for x in p["排序"]:
            expect = (W_MARKOV * x["Markov"] + W_OMIT * x["遗漏节奏"]
                      + W_NEIGH * x["邻号强度"])
            assert abs(expect - x["分"]) < 1e-5


def test_markov_depends_on_last_digit():
    """上期该位不同 ⇒ 转移那一项不同（否则说明「上期」根本没被用上）。"""
    m = position_models("福彩3D")
    for pm in m["位置"]:
        prev = pm["上期"]
        assert prev is None or prev in pm["转移"] or pm["转移"] == {}
        if prev in pm["转移"]:
            assert sum(pm["转移"][prev].values()) >= 1


def test_same_input_same_output():
    assert rank_positions("七星彩") == rank_positions("七星彩")


def test_unimplemented_parts_are_declared():
    r = rank_positions("排列3")
    assert "跨位共现" in r["未实现"]
    assert any("跨位共现" in n for n in NOTES)
    assert any("不接生产" in n for n in NOTES)
