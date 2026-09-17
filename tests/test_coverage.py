"""
ev/coverage.py 回归测试（撞号度量 + 低重叠剪枝）。

锁住的契约：
  1. 随机基线重叠的解析式（双 36/33、大 25/35、七 6/10+1/15、数字型 1/C(10+d-1,d)）；
  2. 三种重叠口径分支：红蓝=红球交集、七星彩=按位相同、数字型=多重集相同；
  3. 剪枝返回**升序下标**（调用方据此过滤 predicted_sets，保持"号码组数==len(预测号码)"）；
  4. **每策略保底**优先于重叠上限（否则某策略整组被剪，归因/权重学习会失真）；
  5. 数字型固定赔率不剪枝（只度量）；`enabled=False` 一键回退。
"""
import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ev.coverage import (coverage_stats, default_cap, effective_count, pair_overlap,
                         prune_predicted_sets, random_baseline_overlap, ticket_key)

STRATS = ["高频策略", "遗漏值策略", "区间均衡策略"]


def _rb(reds, strategy="高频策略"):
    return {"红球": sorted(reds), "蓝球": [1], "策略": strategy}


def _qxc(seventh, first_six, strategy="高频策略"):
    nums = list(first_six) + [seventh]
    return {"号码": {f"第{i + 1}位": [n] for i, n in enumerate(nums)}, "策略": strategy}


def _d3(a, b, c, strategy="高频策略"):
    return {"号码": {"第1位": [a], "第2位": [b], "第3位": [c]}, "策略": strategy}


# ------------------------------------------------------------
# 1. 随机基线解析式
# ------------------------------------------------------------

def test_random_baseline_overlap():
    assert random_baseline_overlap("双色球") == pytest.approx(36 / 33)
    assert random_baseline_overlap("大乐透") == pytest.approx(25 / 35)
    assert random_baseline_overlap("七星彩") == pytest.approx(6 / 10 + 1 / 15)
    assert random_baseline_overlap("福彩3D") == pytest.approx(1 / math.comb(12, 3))
    assert random_baseline_overlap("排列5") == pytest.approx(1 / math.comb(14, 5))


def test_default_cap_matches_selection_module():
    """重叠上限必须与 ev/selection._OVERLAP_CAP 同源，避免两套口径。"""
    from ev.selection import _OVERLAP_CAP
    for lot, cap in _OVERLAP_CAP.items():
        assert default_cap(lot) == cap


# ------------------------------------------------------------
# 2. 三种重叠口径
# ------------------------------------------------------------

def test_pair_overlap_redblue():
    a = _rb([1, 2, 3, 4, 5, 6])
    b = _rb([1, 2, 3, 4, 30, 31])
    assert pair_overlap(ticket_key(a, "双色球"), ticket_key(b, "双色球"), "双色球") == 4


def test_pair_overlap_qxc_positional_not_multiset():
    """七星彩按位比较：数字相同但位置不同不算命中。"""
    q1 = _qxc(7, [1, 2, 3, 4, 5, 6])
    q2 = _qxc(7, [1, 2, 3, 9, 9, 9])
    assert pair_overlap(ticket_key(q1, "七星彩"), ticket_key(q2, "七星彩"), "七星彩") == 4
    q3 = _qxc(7, [6, 5, 4, 3, 2, 1])   # 反转（多重集相同、按位全不同）
    assert pair_overlap(ticket_key(q1, "七星彩"), ticket_key(q3, "七星彩"), "七星彩") == 1


def test_pair_overlap_digit_multiset():
    d1 = _d3(1, 2, 3)
    assert pair_overlap(ticket_key(d1, "福彩3D"), ticket_key(_d3(3, 2, 1), "福彩3D"), "福彩3D") == 1
    assert pair_overlap(ticket_key(d1, "福彩3D"), ticket_key(_d3(5, 5, 5), "福彩3D"), "福彩3D") == 0


# ------------------------------------------------------------
# 3~4. 剪枝契约
# ------------------------------------------------------------

def _batch_100():
    """60 张共用热号池 + 40 张随机，三策略轮转（模拟真实策略趋同）。"""
    rng = random.Random(7)
    out = []
    for i in range(60):
        reds = rng.sample(range(1, 10), 4) + rng.sample(range(10, 34), 2)
        out.append(_rb(reds, STRATS[i % 3]))
    for i in range(40):
        out.append(_rb(rng.sample(range(1, 34), 6), STRATS[i % 3]))
    return out


def test_prune_returns_ascending_unique_indices():
    kept, kpi = prune_predicted_sets(_batch_100(), "双色球", nominal_n=100)
    assert kept == sorted(kept)
    assert len(set(kept)) == len(kept)
    assert 1 <= len(kept) <= 100
    assert kpi["名义注数"] == 100
    assert kpi["剪枝后注数"] == len(kept)
    assert kpi["减注等效"] == 100 - len(kept)
    assert kpi["剪枝开关"] is True


def test_prune_keeps_every_strategy_and_never_drops_one():
    """同一策略内所有票都撞号时，也必须为该策略保底 1 注（保底优先于 cap）。"""
    tickets = [_rb([1, 2, 3, 4, 5, 6], "高频策略"),
               _rb([1, 2, 3, 4, 5, 6], "遗漏值策略")]   # 红球完全相同 → 重叠 6 > cap 4
    kept, kpi = prune_predicted_sets(tickets, "双色球", nominal_n=2)
    assert len(kept) == 2
    assert kpi["策略覆盖数"] == 2


def test_prune_enforces_cap_within_single_strategy():
    """同一策略内超过上限的票应被剪掉（七星彩按位 4 > cap 3）。"""
    tickets = [_qxc(7, [1, 2, 3, 4, 5, 6]), _qxc(7, [1, 2, 3, 9, 9, 9])]
    kept, kpi = prune_predicted_sets(tickets, "七星彩", nominal_n=2)
    assert len(kept) == 1
    assert kpi["平均两两重叠"] == pytest.approx(0.0)   # 只剩一张 → 无两两对


def test_enabled_false_is_full_rollback():
    tickets = _batch_100()
    kept, kpi = prune_predicted_sets(tickets, "双色球", enabled=False)
    assert len(kept) == 100
    assert kpi["剪枝开关"] is False
    assert kpi["减注等效"] == 0


def test_honest_note_present():
    _kept, kpi = prune_predicted_sets(_batch_100(), "双色球")
    assert "不提升任何单注中奖概率" in kpi["诚实声明"]


# ------------------------------------------------------------
# 5. 数字型：不剪枝，但能度量撞号
# ------------------------------------------------------------

def test_digit_lottery_not_pruned():
    tickets = [_d3(1, 2, 3), _d3(3, 2, 1), _d3(5, 5, 5)]
    kept, kpi = prune_predicted_sets(tickets, "福彩3D", nominal_n=3)
    assert len(kept) == 3
    assert kpi["剪枝开关"] is False


def test_digit_effective_count_detects_multiset_clash():
    """纯度量口径下，(1,2,3) 与 (3,2,1) 视为撞号 → 等效 2 注。"""
    tickets = [_d3(1, 2, 3), _d3(3, 2, 1), _d3(5, 5, 5)]
    assert effective_count("福彩3D", tickets) == 2


# ------------------------------------------------------------
# KPI 形状
# ------------------------------------------------------------

def test_coverage_stats_shape():
    st = coverage_stats("大乐透", [_rb([1, 2, 3, 4, 5]), _rb([1, 2, 3, 4, 6])])
    assert st["名义注数"] == 2
    assert st["剪枝后注数"] == 2
    assert st["平均两两重叠"] == pytest.approx(4.0)
    # KPI 是展示口径：基线重叠按 4 位小数定稿（大 25/35 → 0.7143）
    assert st["随机基线重叠"] == pytest.approx(round(25 / 35, 4))
    for key in ("有效覆盖率", "撞号对占比", "重叠上限cap", "每策略保底", "诚实声明"):
        assert key in st
