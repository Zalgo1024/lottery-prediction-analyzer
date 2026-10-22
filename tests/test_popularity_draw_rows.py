# -*- coding: utf-8 -*-
"""tests/test_popularity_draw_rows.py —— _load_draw_rows 按期号键(年,序)排序的回归锁。

锁住历史 bug：双色球 2003-2009 期号是 4 位（如 "3001"），裸字符串排序会把它排到
2026 的 7 位期号（"2026001"）之后，使「按行序前 N 期」的 CV/walk-forward 切点错位。
"""
import pytest

from data.issue_key import issue_key
from ev import popularity as POP


@pytest.mark.parametrize("lot", ["双色球", "大乐透"])
def test_draw_rows_issues_sorted_by_key(lot):
    """返回的 issues 必须按 (年,序) 非降序；4 位早期期号不得排在后期之后。"""
    rows = POP._load_draw_rows(lot)
    if rows is None:
        pytest.skip(f"{lot} 无足够有效行")
    issues = rows["issues"]
    assert len(issues) >= 2
    keys = [issue_key(lot, s) for s in issues]
    for a, b in zip(keys, keys[1:]):
        assert a <= b, f"{lot} 期号序列未按 (年,序) 升序：{issues[keys.index(a)]} vs {issues[keys.index(b)]}"


def test_ssq_early_4digit_before_recent():
    """双色球：最早的有效期号（2003 年 4 位）必须早于任何 2026 年期号。"""
    rows = POP._load_draw_rows("双色球")
    if rows is None:
        pytest.skip("双色球 无足够有效行")
    keys = [(str(s), issue_key("双色球", s)) for s in rows["issues"]]
    early = [s for s, k in keys if k[0] <= 2003]
    recent = [s for s, k in keys if k[0] >= 2026]
    assert early and recent, "缺少早期或近期样本"
    # 最早早期期号的键 < 最晚近期期号的键（2003 ≪ 2026）
    assert issue_key("双色球", early[0]) < issue_key("双色球", recent[-1])
    # 且早期期号不得出现在近期期号之后（字符串排序会破坏这一点，期号键不会）
    first_recent_pos = min(i for i, (s, k) in enumerate(keys) if k[0] >= 2026)
    last_early_pos = max(i for i, (s, k) in enumerate(keys) if k[0] <= 2003)
    assert last_early_pos < first_recent_pos, "早期 4 位期号排在 2026 期号之后（裸字符串排序 bug 复现）"
