# -*- coding: utf-8 -*-
"""C5 展示层清副本 —— 固定测试。

覆盖四件事：
1. `ev/rollover.fixed_prizes_for` 按规则版本生成（双色球与旧手写值对账；
   大乐透五段金额/级数正确；2014 及以前三等浮动不进固定表）。
2. `pipeline/step1_probability.prize_probabilities_dlt` 按规则版本生成
   （与 `test_dlt_versions` 的 13 条件手算金值一致）。
3. `ev/profit/rules.grade_rank_names` 单一真源（一~九等，不含数字型奖级）。
4. `ev/profit/payouts` 三等浮动份额仅 2014 段生效（build_ctx / note 按版本过滤）。

只读纯函数 + 只读 market.db，不写任何数据文件。
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.prize_versions import DLT_VERSIONS, version_for_issue
from ev.profit.rules import grade_rank_names, rules_for
from pipeline.step1_probability import prize_probabilities_dlt

# ---------------------------------------------------------------- 1. rollover


def test_ssq_fixed_matches_hand_computed():
    """双色球固定奖与旧手写值逐项对账（防重构改数）。"""
    from ev.rollover import fixed_prizes_for

    c = math.comb
    denom = c(33, 6) * 16
    expect = {
        "三等": (3000, c(6, 5) * c(27, 1) / denom),
        "四等": (200, (c(6, 5) * c(27, 1) * 15 + c(6, 4) * c(27, 2)) / denom),
        "五等": (10, (c(6, 4) * c(27, 2) * 15 + c(6, 3) * c(27, 3)) / denom),
        "六等": (5, (c(6, 2) * c(27, 4) + c(6, 1) * c(27, 5) + c(27, 6)) / denom),
    }
    got = fixed_prizes_for("双色球")
    assert set(got) == set(expect)
    for g, (amt, p) in expect.items():
        assert got[g][0] == amt
        assert got[g][1] == pytest.approx(p, rel=1e-15)


@pytest.mark.parametrize("issue,version,levels,fixed", [
    ("07001", "dlt-2007-05", 5, {"四等": 3000, "五等": 500, "六等": 200, "七等": 10, "八等": 5}),
    ("09121", "dlt-2009-10", 5, {"四等": 3000, "五等": 600, "六等": 100, "七等": 10, "八等": 5}),
    ("14052", "dlt-2014-05", 3, {"四等": 200, "五等": 10, "六等": 5}),
    ("19019", "dlt-2019-02", 7, {"三等": 10000, "四等": 3000, "五等": 300, "六等": 200,
                                 "七等": 100, "八等": 15, "九等": 5}),
    ("26014", "dlt-2026-02", 5, {"三等": 5000, "四等": 300, "五等": 150, "六等": 15, "七等": 5}),
])
def test_dlt_fixed_by_version(issue, version, levels, fixed):
    """大乐透五段固定奖：级数、金额、版本名全部对上。"""
    from ev.rollover import fixed_prizes_for, rules_version_of

    got = fixed_prizes_for("大乐透", issue)
    assert rules_version_of("大乐透", issue) == version
    assert len(got) == levels
    assert {g: a for g, (a, _) in got.items()} == fixed
    # 13 个中奖条件的概率和跨段守恒（几何不变，只有奖级划分在变）
    assert sum(p for _, p in got.values()) == pytest.approx(0.066704, abs=1e-5)


def test_dlt_third_floating_before_2019():
    """2014 及以前三等是浮动奖 ⇒ 不进固定表；2019 起是固定奖 ⇒ 进表。"""
    from ev.rollover import fixed_prizes_for

    assert "三等" not in fixed_prizes_for("大乐透", "14052")
    assert "三等" not in fixed_prizes_for("大乐透", "09121")
    assert "三等" in fixed_prizes_for("大乐透", "19019")
    assert "三等" in fixed_prizes_for("大乐透", "26015")


def test_fixed_prize_ev_issue_aware():
    """fixed_prize_ev 按期结算；2026 与 2019 段固定奖贡献不同。"""
    from ev.rollover import fixed_prize_ev, fixed_prizes_robust

    ev26 = fixed_prize_ev("大乐透", "26015")
    ev19 = fixed_prize_ev("大乐透", "19019")
    assert ev26 != ev19
    # 不可解析期号 → 现行版本（显式降级路径不炸）
    assert fixed_prizes_robust("大乐透", "TEST") == fixed_prizes_robust("大乐透", None)
    # _FIXED_PRIZES 兼容导入点 = 现行版
    from ev.rollover import _FIXED_PRIZES
    assert _FIXED_PRIZES["大乐透"] == fixed_prizes_robust("大乐透", None)


# ---------------------------------------------------------------- 2. step1

def test_step1_dlt_levels_by_version():
    """step1 大乐透概率表：级数/条件/金额随版本；金值与手算一致。"""
    cur = prize_probabilities_dlt()
    assert [lv["prize_name"] for lv in cur["levels"]] == \
        ["一等", "二等", "三等", "四等", "五等", "六等", "七等"]
    assert cur["total_combinations"] == 21_425_712
    lv = {x["prize_name"]: x for x in cur["levels"]}
    assert lv["三等"]["condition"] == "5+0 / 4+2" and lv["三等"]["fixed_amount"] == 5000
    assert lv["五等"]["condition"] == "4+0 / 3+2" and lv["五等"]["fixed_amount"] == 150
    assert lv["一等"]["is_floating"] and lv["二等"]["is_floating"]

    d19 = {x["prize_name"]: x for x in prize_probabilities_dlt("19019")["levels"]}
    assert len(d19) == 9
    assert d19["四等"]["win_combs"] == 150 and d19["四等"]["fixed_amount"] == 3000
    assert d19["八等"]["win_combs"] == 127600      # 3+1(87000) + 2+2(40600)
    assert d19["九等"]["win_combs"] == 1287281     # 195750+812000+137025+142506

    d14 = {x["prize_name"]: x for x in prize_probabilities_dlt("14052")["levels"]}
    assert len(d14) == 6
    assert d14["三等"]["is_floating"] and d14["三等"]["win_combs"] == 195
    assert d14["四等"]["win_combs"] == 7350 and d14["四等"]["fixed_amount"] == 200
    assert d14["六等"]["win_combs"] == 1287281

    d07 = {x["prize_name"]: x for x in prize_probabilities_dlt("07001")["levels"]}
    assert len(d07) == 8
    assert d07["四等"]["fixed_amount"] == 3000 and d07["五等"]["fixed_amount"] == 500
    assert d07["七等"]["win_combs"] == 127600
    assert d07["八等"]["win_combs"] == 1287281


def test_step1_total_win_probability_conserved():
    """13 个中奖条件的总概率跨段守恒（0.066704）。"""
    for issue in ("07001", "09121", "14052", "19019", "26015"):
        d = prize_probabilities_dlt(issue)
        assert d["total_win_probability"] == pytest.approx(0.066704, abs=1e-5)
        assert abs(sum(lv["probability"] for lv in d["levels"])
                   - d["total_win_probability"]) < 1e-12


# ---------------------------------------------------------------- 3. 排序权重

def test_grade_rank_names_single_source():
    """一~九等升序；数字型奖级不入表；与规则表并集一致。"""
    names = grade_rank_names()
    assert names == ("一等", "二等", "三等", "四等", "五等", "六等", "七等", "八等", "九等")
    # 与规则表实际出现的奖级一致（不含数字型）。
    # ★ _RULES 键为 (彩种, 玩法) 元组（快乐8 多玩法），直接遍历 items，
    #   不要拿 SUPPORTED（彩种名）去做 _RULES[彩种名] 索引。
    from ev.profit.rules import _RULES
    live = {g for (_lot, _play), rs in _RULES.items()
            for r in rs for g in r.grades if g.endswith("等")}
    assert set(names) == live
    assert "直选" not in names and "组选3" not in names


# ---------------------------------------------------------------- 4. 浮动份额

def test_float_share_third_grade_only_before_2019():
    """大乐透三等浮动份额：仅 2014 及更早段生效（该段三等为浮动奖）。"""
    from ev.profit.payouts import FLOAT_SHARE, build_ctx, note

    assert FLOAT_SHARE["大乐透"]["三等"][0] == 0.07     # 2014 财政部公告原文

    for issue, has3 in (("14052", True), ("09121", True), ("19019", False), ("26015", False)):
        ctx = build_ctx("大乐透", issue)
        floating = set(ctx.rules.floating())
        assert ("三等" in ctx.float_pool) == has3
        assert set(ctx.float_pool) == floating           # 无幽灵条目
        assert set(note(ctx)["浮动奖份额"]) == floating   # note 只报当期浮动奖级

    # 双色球不受影响
    ctx = build_ctx("双色球", "26120")
    assert set(ctx.float_pool) == {"一等", "二等"}
