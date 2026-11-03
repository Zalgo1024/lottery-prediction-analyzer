# -*- coding: utf-8 -*-
"""tests/test_arena_periods.py —— 期窗是不变量：不许求交、取众数、截断。"""
from __future__ import annotations

import pytest

from ev.arena import periods as PD


def test_build_periods_ascending_numeric():
    iss = PD.build_periods("双色球", 200)
    assert len(iss) == 200
    # ★ 期号按**数值**升序（2003~2009 是一位年 3001…，跨年重编号 26281→27001）
    nums = [int(x) for x in iss]
    assert nums == sorted(nums)
    assert nums[-1] > nums[0]


def test_full_window_matches_known_size():
    """双色球全量期数（Stage 0 实测值起钉的回归钉；新开奖入库时随数据同步提交一并 bump）。

    2026-10-25：26122 期开奖入库 ⇒ 3518 → 3519。
    2026-10-29：26123 期开奖入库 ⇒ 3519 → 3520。
    2026-10-30：26124 期开奖入库 ⇒ 3520 → 3521。
    2026-11-01：26125 期开奖入库 ⇒ 3521 → 3522（★该期奖级数据尚未公布，
                CSV 里一等奖注数/奖金仍为 0，是「号码已出、奖级待公布」的常态行）。
    2026-11-03：26126 期开奖入库 ⇒ 3522 → 3523（★奖级待公布，同上常态）。
    ★ 此钉防的是 `build_periods` 的窗口构造回归（求交/截断/丢期），不是永久冻结期数；
      每夜 23:10 自动更新会按期撞它，bump 时必须与数据提交同进（否则 CI 红）。
    """
    assert len(PD.build_periods("双色球")) == 3523


def test_assert_same_periods_ok_on_equal():
    a = [1, 2, 3]
    PD.assert_same_periods(a, list(a))          # 不抛


@pytest.mark.parametrize("b", [[1, 2], [1, 2, 4], [1, 2, 3, 4], []])
def test_assert_same_periods_raises_on_any_diff(b):
    """长度不同 / 内容不同 / 空 —— 一律抛。★ 不允许静默截断到较短的那个。"""
    with pytest.raises(ValueError, match="期窗不一致"):
        PD.assert_same_periods([1, 2, 3], b)


def test_exclude_rule_versions_changes_window():
    """剔除规则版本必须真的减少期数（大乐透有 5 段）。"""
    full = PD.build_periods("大乐透")
    cut = PD.build_periods("大乐透", exclude_rule_versions=["dlt-2007-05"])
    assert len(cut) < len(full)
    assert set(cut) < set(full)


def test_profile_counts_rule_versions():
    p = PD.profile("大乐透", PD.build_periods("大乐透"))
    assert p["规则版本数"] >= 2          # 大乐透多版本（实测 5 段）
    assert sum(p["规则版本分布"].values()) == p["期数"]
    p1 = PD.profile("双色球", PD.build_periods("双色球", 500))
    assert p1["规则版本数"] == 1         # 双色球只有 1 个版本
