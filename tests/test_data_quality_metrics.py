# -*- coding: utf-8 -*-
"""数据质量评分的**完整性口径**测试（credibility/data_quality.py）。

锁死 2026-10-23 修掉的三处「罚正常现象」：
  ① 期号每年重编号 → 直接把 `26281 → 27001` 当成缺期（实测每彩种虚报 19~23 处）；
  ② 重复开奖是**生日悖论**（3D 只有 1000 种组合、7788 期，必然 6788 期重复），
     原注释还写死「概率约 1/千万级」，对数字彩完全错误；
  ③ 日期间隔 >7 天 ≈ 春节休市（22~24 处 ≈ 历史年数），不是完整性缺陷。

真实数据源（本地 CSV），与 tests/test_randomness_audit.py 同风格。
"""

import math

import pytest

from credibility.data_quality import (
    _DUP_PENALTY_Z,
    _expected_duplicates,
    _outcome_space,
    data_quality_report,
)
from data.issue_key import issue_key
from data.loader import load_lottery


# ------------------------------------------------------------
# 组合空间 / 生日悖论基线
# ------------------------------------------------------------

@pytest.mark.parametrize("lot,space", [
    ("双色球", 17_721_088),      # C(33,6) × 16
    ("大乐透", 21_425_712),      # C(35,5) × C(12,2)
    ("排列5", 100_000),          # 10^5
    ("排列3", 1_000),            # 10^3
    ("福彩3D", 1_000),           # 10^3
    ("七星彩", 15_000_000),      # 10^6 × 15
])
def test_outcome_space_matches_known(lot, space):
    assert _outcome_space(lot) == space


def test_expected_duplicates_3d_is_almost_certain():
    """3D：7788 期 / 1000 种组合 ⇒ 基线 6788.4 期重复，几乎是必然。"""
    base = _expected_duplicates(7788, 1000)
    assert base is not None
    assert abs(base - 6788.4) < 1.0
    # 关键：重复数几乎等于「期数 − 组合数」
    assert 7788 - 1000 - 2 < base < 7788 - 1000 + 2


def test_expected_duplicates_lotto_space_is_near_zero():
    """双色球：3518 期 / 1772 万种组合 ⇒ 基线 0.3 期，出现 0 期完全正常。"""
    base = _expected_duplicates(3518, 17_721_088)
    assert base is not None
    assert 0.2 < base < 0.5


def test_expected_duplicates_handles_unknown_space():
    assert _expected_duplicates(100, None) is None
    assert _expected_duplicates(100, 0) is None
    assert _expected_duplicates(0, 100) is None


def test_dup_penalty_threshold_is_strict():
    """只在显著高于基线时才扣分：生日悖论本身不能当成数据错误。"""
    assert _DUP_PENALTY_Z >= 4.0


# ------------------------------------------------------------
# 期号断裂：必须按 (年, 序号) 归一
# ------------------------------------------------------------

def test_issue_gap_raw_integer_diff_is_meaningless():
    """反证：若直接比整数期号，跨年会被当成缺期（这就是原 bug）。"""
    # 双色球 2025 年末 → 2026 年初：期号整数差 720，归一后是相邻年份的年初
    assert 26001 - 25281 > 1
    assert issue_key("双色球", "25281")[0] == 2025
    assert issue_key("双色球", "26001")[0] == 2026


def test_issue_key_year_rollover_is_not_a_gap():
    """归一后：跨年 ⇒ 不参与断裂判定；同年内连续 ⇒ 序号差 1。"""
    y1, s1 = issue_key("双色球", "25281")
    y2, s2 = issue_key("双色球", "26001")
    assert y1 != y2                     # 跨年 ⇒ 跳过，不算断裂
    y3, s3 = issue_key("双色球", "26002")
    assert y2 == y3 and s3 - s2 == 1     # 同年内连续 ⇒ 无缺期


@pytest.mark.parametrize("lot,expected", [
    ("双色球", 0),
    ("大乐透", 0),
    ("排列3", 0),
    ("福彩3D", 0),
    ("七星彩", 0),
])
def test_no_real_issue_gaps(lot, expected):
    """五个彩种期号完全连续（跨年重编号不算）。"""
    r = data_quality_report(lot)
    assert r["issue_gaps"] == expected, r["issue_gap_samples"]


def test_paile5_has_two_real_gaps():
    """排列5 真缺 04006/04007 两期（销售奖级数据里有，历史号码没抓到）。"""
    r = data_quality_report("排列5")
    assert r["issue_gaps"] == 2, r["issue_gap_samples"]
    assert r["issue_gap_samples"][0]["缺"] == 2


# ------------------------------------------------------------
# 评分不再罚正常现象
# ------------------------------------------------------------

def test_3d_duplicates_do_not_penalize_score():
    """3D 6788 期重复是必然，不得因此扣分。"""
    r = data_quality_report("福彩3D")
    assert r["duplicates"] > 6000
    assert r["dup_z"] is not None and abs(r["dup_z"]) < 3.0
    assert r["trust_score"] >= 95.0, r


def test_holiday_date_gaps_do_not_penalize_score():
    """春节休市导致的大日期间隔只报不罚。"""
    r = data_quality_report("双色球")
    assert r["big_date_gaps"] > 15          # 是真实存在的观测
    assert r["trust_score"] >= 95.0, r      # 但不得因此扣分


def test_clean_lottery_scores_full_marks():
    """双色球 0 缺期 0 重复 0 实质拒绝 ⇒ 应接近满分。"""
    r = data_quality_report("双色球")
    assert r["issue_gaps"] == 0
    assert r["duplicates"] == 0
    assert r["n_rejected"] == 0
    assert r["trust_score"] >= 99.0, r


def test_notes_never_claim_fixed_collision_probability():
    """备注里不许再写死「1/千万级」——数字彩组合空间只有 1e3~1e5。"""
    for lot in ("双色球", "福彩3D", "排列5"):
        r = data_quality_report(lot)
        joined = " ".join(r["notes"])
        assert "千万级" not in joined
        if r["duplicates"]:
            assert "生日悖论" in joined


def test_dup_note_states_baseline_and_space():
    """重复开奖备注必须同时给出观测值、基线值与组合空间，便于复核。"""
    r = data_quality_report("排列3")
    note = next(n for n in r["notes"] if "重复开奖" in n)
    assert "6757" in note or str(r["duplicates"]) in note
    assert "组合空间" in note
    assert "1,000" in note or "1000" in note
