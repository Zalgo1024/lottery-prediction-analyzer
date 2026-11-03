# -*- coding: utf-8 -*-
"""预测报告「期望收益」段的红线锁（2026-11-02）。

真实缺陷（旧代码，无任何测试覆盖）：

    _compute_group_expected_value() 把浮动奖级（一等/二等奖）一律按
    **500 万元** 计，于是双色球报告打出

        单注期望奖金 5.00 元 / 返奖率 **250.0%** / 评级 **正值（非零和）**

    而同一台机器上 `cli.py payout 双色球` 给出净 EV **−1.22 元**。两个数字互
    相矛盾，且其中一个正面违反「EV 恒负」这条底线。

为什么"填个大数"必然错：二等概率 8.46e-7，单注金额量级 10^4~10^5，按 5e6 计
单项就贡献 4.23 元 —— 比整张彩票的真实期望奖金还高 4 倍。

本文件锁死三件事：
  1) 单注期望奖金 < 成本（即净 EV 恒负），这是**结构性不变量**，与彩种无关；
  2) 评级文案里不得出现「非零和 / 正值」这类利好措辞；
  3) 金额未知时必须返回 None，不得补 0（补 0 会被读成"这部分不产生奖金"）。
"""

import re

import pytest

from prediction.reporter import _compute_group_expected_value


def _rate_pct(text: str) -> float:
    m = re.search(r"([\d.]+)%", text)
    assert m, f"返奖率不是百分数：{text!r}"
    return float(m.group(1))


@pytest.mark.parametrize("lot,reds,blues", [
    ("双色球", [1, 2, 3, 4, 5, 6], [7]),
    ("大乐透", [1, 2, 3, 4, 5], [6, 7]),
])
def test_single_ticket_ev_is_negative(lot, reds, blues):
    """净 EV 恒负 —— 这是彩票的结构性不变量，任何口径都不该算出正值。"""
    r = _compute_group_expected_value(reds, blues, lot)
    assert r["expected_prize"] is not None
    assert r["expected_return"] is not None
    assert r["expected_return"] < 0, f"{lot} 算出非负期望：{r}"
    assert r["expected_prize"] < r["ticket_price"]
    assert _rate_pct(r["return_rate"]) < 100


@pytest.mark.parametrize("lot,reds,blues", [
    ("双色球", [1, 2, 3, 4, 5, 6], [7]),
    ("大乐透", [1, 2, 3, 4, 5], [6, 7]),
])
def test_grade_never_claims_positive_ev(lot, reds, blues):
    """评级文案不得把正期望包装成「非零和」这种利好。"""
    r = _compute_group_expected_value(reds, blues, lot)
    assert "非零和" not in r["grade"]
    assert "正值" not in r["grade"]
    assert "负值" in r["grade"]


def test_floating_grades_are_not_hardcoded_to_five_million():
    """浮动奖不得按 500 万硬编码：真源口径下双色球单注期望奖金远低于 2 元。

    旧实现给「一等 5e6 × 5.64e-8 + 二等 5e6 × 8.46e-7」= 4.51 元，单这一项就
    超过整票成本。这里用「期望奖金 < 成本」把该口径彻底排除。
    """
    r = _compute_group_expected_value([1, 2, 3, 4, 5, 6], [7], "双色球")
    assert r["expected_prize"] < 2.0, r


def test_unknown_lottery_reports_unknown_not_zero():
    """金额算不出时必须是 None + 显式文案，不能静默当 0。"""
    r = _compute_group_expected_value([1, 2, 3], [1], "不存在的彩种")
    assert r["expected_prize"] is None
    assert r["expected_return"] is None
    assert "无法计算" in r["grade"]
    assert r["return_rate"] == "—"


def test_issue_is_reported_so_reader_knows_it_is_period_level():
    """报告必须写明期号：这是「该期」的 EV（含当期满池/分薄），不是长期期望。"""
    r = _compute_group_expected_value([1, 2, 3, 4, 5, 6], [7], "双色球")
    assert r["issue"], "缺少期号，读者会误读成长期期望"
