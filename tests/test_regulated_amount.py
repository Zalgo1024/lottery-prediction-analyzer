# -*- coding: utf-8 -*-
"""regulated 档的单注金额取值链：当期实测优先 → 名义回退。

★ 为什么要专门测：快乐8 的 39 档在 market.db 里**每档都有多个金额值**——
  x7z7 名义 1 万、2026 年是 8500；x9z9 名义 30 万、2026 年有 220 期是 25 万；
  x10z10 有 173 期 0 元、1101 期无人中出。
  一律按名义值算会把返奖率算得偏高，一律按 fixed 算则连"限赔"这件事都不存在。
"""
from __future__ import annotations

import pytest

from data.prize_amounts import normalize_level_name
from ev.profit.amounts import (
    SRC_ACTUAL,
    SRC_FIXED,
    SRC_NOMINAL,
    SRC_NO_GRADE,
    grade_amount,
)

pytestmark = pytest.mark.realdata


# ---------------------------------------------------------------- 固定档
def test_qlc_fixed_grades_use_table():
    for g, amt in (("四等", 200), ("五等", 50), ("六等", 10), ("七等", 5)):
        got, src = grade_amount("七乐彩", "2026125", g)
        assert (got, src) == (float(amt), SRC_FIXED)


def test_grade_name_normalized():
    """判级层给「六等奖」、规则表键是「六等」—— 必须走同一归一。"""
    assert grade_amount("七乐彩", "2026125", "六等奖") == (10.0, SRC_FIXED)


# ---------------------------------------------------------------- 实测优先
def test_qlc_first_prize_uses_actual():
    got, src = grade_amount("七乐彩", "2026125", "一等")
    assert src == SRC_ACTUAL
    assert got == pytest.approx(1_324_430, rel=1e-6)


def test_kl8_jackpot_uses_actual():
    got, src = grade_amount("快乐8", "2026290", "选十中十")
    assert (got, src) == (5_000_000.0, SRC_ACTUAL)


def test_kl8_capped_grade_uses_actual_not_nominal():
    """x9z9 名义 30 万，2026 年是 25 万（限赔）—— 用名义会高估 20%。"""
    got, src = grade_amount("快乐8", "2026290", "选九中九")
    assert (got, src) == (250_000.0, SRC_ACTUAL)


def test_kl8_limited_grade_uses_actual_not_nominal():
    """x7z7 名义 1 万，2026 年是 8500。"""
    got, src = grade_amount("快乐8", "2026290", "选七中七")
    assert (got, src) == (8_500.0, SRC_ACTUAL)


def test_kl8_full_amount_period():
    """2025 年的 x9z9 是全额 30 万 —— 同一档同一年份内也会变。"""
    got, src = grade_amount("快乐8", "2025349", "选九中九")
    assert (got, src) == (300_000.0, SRC_ACTUAL)


# ---------------------------------------------------------------- 名义回退
def test_kl8_no_winner_period_falls_back_to_nominal():
    """2026288 期 x10z10 金额为 0（无人中出）⇒ 查不到 ⇒ 回退名义值并**标注**。

    ★ 关键在于「标注」：5,000,000 这个数字本身与实测期完全一样，
      不标来源的话，账本里"有人中出"与"没人中出"长得一模一样。
    """
    got, src = grade_amount("快乐8", "2026288", "选十中十")
    assert (got, src) == (5_000_000.0, SRC_NOMINAL)


# ---------------------------------------------------------------- 纪律
def test_unknown_grade_returns_none_not_zero():
    """未知奖级必须给 None，不能给 0 —— 0 会被当成"中了但没钱"。"""
    got, src = grade_amount("快乐8", "2026290", "选十一中十一")
    assert got is None
    assert src == SRC_NO_GRADE


@pytest.mark.parametrize("raw,expect", [
    ("x10z10", "选十中十"), ("x9z9", "选九中九"),
    ("x1z1", "选一中一"), ("x10z0", "选十中零"), ("x4z2", "选四中二"),
])
def test_level_name_normalization(raw, expect):
    assert normalize_level_name(raw)[0] == expect
