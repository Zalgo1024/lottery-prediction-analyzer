# -*- coding: utf-8 -*-
"""快乐8：10 种玩法、39 档奖级、超几何概率。

与既有 6 彩种的根本差别：**开奖摇 20 个，票面按玩法选 1~10 个**。
所以 `model="hypergeom"`、`draw_size=20`，且结局数不可枚举。
"""
from __future__ import annotations

import math

import pytest

from data.prize_table import REGULATED_CAP, REGULATED_NOMINAL
from ev.payout import grade_probabilities
from ev.profit import rules as R

PLAYS = list(R.KL8_PLAYS)


# ---------------------------------------------------------------- 结构
def test_ten_plays_cover_39_grades():
    """10 种玩法各一张奖表，合计 39 档 —— 与 market.db 的 level_name 数一致。"""
    total = sum(len(R.rules_for("快乐8", play=p).grade_table) for p in PLAYS)
    assert total == 39
    assert len(PLAYS) == 10


@pytest.mark.parametrize("play,n", sorted(R.KL8_PLAYS.items(), key=lambda x: x[1]))
def test_play_pick_count(play, n):
    r = R.rules_for("快乐8", play=play)
    assert r.outer_width == n
    assert r.draw_size == 20
    assert r.model == "hypergeom"
    assert r.play == play


def test_default_play_is_ten():
    assert R.rules_for("快乐8").play == "选十"


def test_unknown_play_rejected():
    with pytest.raises(KeyError, match="无此玩法"):
        R.rules_for("快乐8", play="选十一")


def test_all_grades_are_regulated():
    """39 档没有一档是真固定：x7z7 名义 1 万、2026 年出现 8500；
    x9z9 名义 30 万、2026 年有 220 期是 25 万；x10z10 有 173 期 0 元、1101 期无人中出。"""
    r = R.rules_for("快乐8", play="选十")
    assert all(g.kind == "regulated" for g in r.grades.values())
    assert r.grades["选十中十"].cap == 5_000_000
    assert REGULATED_CAP["快乐8"] == {"选十中十": 5_000_000}


def test_nominal_table_covers_every_grade():
    names = {g for p in PLAYS for g in R.rules_for("快乐8", play=p).grade_table.values()}
    assert names == set(REGULATED_NOMINAL["快乐8"])
    assert len(names) == 39


def test_outcome_size_refuses():
    with pytest.raises(NotImplementedError):
        R.rules_for("快乐8", play="选十").outcome_size


# ---------------------------------------------------------------- 概率
def test_ten_of_ten_probability():
    """选十中十 = C(20,10)/C(80,10) = 1.1221e-07。

    ★ 若把分母写成 C(80,20)=3.5e18，会得到 5.2e-14（偏小 215 万倍），
      形状仍是超几何、肉眼完全分辨不出 —— 所以这条要钉死。
    """
    p = grade_probabilities("快乐8", play="选十")
    assert p["选十中十"] == pytest.approx(1.1221e-7, rel=1e-4)
    assert math.comb(80, 10) == 1_646_492_110_120


@pytest.mark.parametrize("k", [0, 5, 6, 7, 8, 9, 10])
def test_ten_play_probabilities(k):
    p = grade_probabilities("快乐8", play="选十")
    name = R.kl8_grade_name("选十", k)
    expect = math.comb(20, k) * math.comb(60, 10 - k) / math.comb(80, 10)
    assert p[name] == pytest.approx(expect, rel=1e-12)


@pytest.mark.parametrize("play", PLAYS)
def test_each_play_probabilities_sum_to_hypergeom(play):
    """每个玩法的命中分布必须与超几何完全一致（与奖表无关）。"""
    n = R.KL8_PLAYS[play]
    p = grade_probabilities("快乐8", play=play)
    # 有奖档的概率 + 无奖档的概率 = 1
    total = sum(p.values())
    assert 0 < total < 1
    # 逐档核对
    for name, val in p.items():
        k = R.rules_for("快乐8", play=play).grade_table
        kk = [kk for (kk, _j), g in k.items() if g == name][0]
        assert val == pytest.approx(
            math.comb(20, kk) * math.comb(60, n - kk) / math.comb(80, n), rel=1e-12)


@pytest.mark.parametrize("play", PLAYS)
def test_return_rate_range(play):
    """官方名义返奖率全部落在 57.1%~58.1%（选四最高 58.02%）。"""
    r = R.rules_for("快乐8", play=play)
    p = grade_probabilities("快乐8", play=play)
    ev = sum(p[g] * r.grades[g].nominal for g in p)
    assert 0.57 <= ev / 2 <= 0.581, f"{play} 返奖率 {ev / 2:.4%}"


def test_ten_play_ev_half_comes_from_jackpot():
    """★ 诚实边界必须可量化：选十的名义 EV 有近一半挂在「中十」这一档，
    而该档每期有 173/2111 期是 0 元、1101 期无人中出。"""
    r = R.rules_for("快乐8", play="选十")
    p = grade_probabilities("快乐8", play="选十")
    ev = sum(p[g] * r.grades[g].nominal for g in p)
    top = p["选十中十"] * 5_000_000
    assert ev == pytest.approx(1.1505, abs=1e-3)
    assert top / ev > 0.48


# ---------------------------------------------------------------- 判级
@pytest.mark.parametrize("hits,expect", [
    (10, "选十中十"), (5, "选十中五"), (0, "选十中零"),
    (9, "选十中九"), (4, None), (1, None),
])
def test_grade_of_hits_ten(hits, expect):
    assert R.grade_of_hits("快乐8", hits) == expect


@pytest.mark.parametrize("hits,expect", [(9, "选九中九"), (0, "选九中零"),
                                         (4, "选九中四"), (3, None)])
def test_grade_of_hits_nine(hits, expect):
    assert R.grade_of_hits("快乐8", hits, play="选九") == expect


# ---------------------------------------------------------------- 与库对账
@pytest.mark.realdata
def test_grade_names_match_market_db():
    """39 档名字必须与 market.db 的 level_name（x10z10 形态）**双向相等**。

    少一档 = 该档实测金额查不到，会静默回退名义值；多一档 = 规则表里有
    官方根本不派奖的档位。
    """
    import sqlite3
    from pathlib import Path

    db = Path(__file__).resolve().parents[1] / "market" / "market.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = {r[0] for r in con.execute(
            "SELECT DISTINCT level_name FROM prize WHERE lottery='kl8'").fetchall()}
    finally:
        con.close()

    from data.prize_amounts import normalize_level_name
    db_names = {normalize_level_name(r)[0] for r in rows}
    ours = {g for p in PLAYS for g in R.rules_for("快乐8", play=p).grade_table.values()}
    assert ours == db_names
    assert len(ours) == 39


# ---------------------------------------------------------------- 返奖率锚点
@pytest.mark.parametrize("play", PLAYS)
def test_nominal_return_rate_is_about_57_5_percent(play):
    """★ 快乐8 的**名义**返奖率约 57.5%，显著高于双色球/大乐透的约 50%。

    这不是本仓的估算，而是《快乐8游戏规则》设定的奖金结构：
    10 个玩法的名义 (Σ p×名义单注)/2 全部落在 57.1%~58.1%。
    实测金额（限赔后）只会更低，不会更高 ⇒ 这是一条**上界**判据。

    ★ 为什么值得钉住：项目 README 长期写「单注长期 EV ≈ −50%」，
      那只对 50% 返奖率的彩种成立。接入快乐8 后若不改口径，
      "≈ −50%" 会变成一句对其中一个彩种明显偏悲观的话。
    """
    p = grade_probabilities("快乐8", play=play)
    r = R.rules_for("快乐8", play=play)
    nominal_ev = sum(pp * (r.grades[g].nominal or 0.0) for g, pp in p.items())
    rate = nominal_ev / 2.0
    assert 0.571 <= rate <= 0.581, (play, rate)
    # 净 EV 必为负（诚实底线）
    assert nominal_ev - 2.0 < 0


def test_qlc_nominal_return_rate_below_one():
    """七乐彩：一~三等浮动，名义值取自奖池分布，长期返奖率仍 < 1（此处只守上界）。"""
    from ev.payout import payout_eff
    r = payout_eff("七乐彩", {"号码": {"基本号": [1, 2, 3, 4, 5, 6, 7], "特别号": []}})
    assert "error" not in r
    assert r["净EV"] < 0
