# -*- coding: utf-8 -*-
"""tests/test_dlt_versions.py —— 大乐透五段规则表（单一真源）。

★ 本文件的金值**手算**（不 import 生产代码的推导），否则就是自证。
★ 13 个中奖条件跨 2014+ 三段恒定；2014 前是 8 级、共 13 条也要对上。
"""
import math

import pytest

from data.issue_key import issue_key
from data.prize_versions import (
    DLT_VERSIONS,
    RuleVersionError,
    classify_dlt,
    fixed_amount,
    uplift_applies,
    version_by_name,
    version_for_issue,
    version_for_key,
)

EXPECT_LEVELS = {
    "dlt-2007-05": 8,
    "dlt-2009-10": 8,
    "dlt-2014-05": 6,
    "dlt-2019-02": 9,
    "dlt-2026-02": 7,
}


def test_five_versions_in_order():
    assert [v.version for v in DLT_VERSIONS] == list(EXPECT_LEVELS)
    assert all(DLT_VERSIONS[i].from_key < DLT_VERSIONS[i + 1].from_key
               for i in range(len(DLT_VERSIONS) - 1))


@pytest.mark.parametrize("name,levels", EXPECT_LEVELS.items())
def test_level_count(name, levels):
    v = version_by_name(name)
    assert v.level_count == levels, f"{name} 应有 {levels} 级"


@pytest.mark.parametrize("name", list(EXPECT_LEVELS))
def test_thirteen_winning_conditions(name):
    """13 个中奖条件跨段守恒（2014 前 8 级也是 13 条）。"""
    v = version_by_name(name)
    assert len(v.grade_table) == 13, f"{name} 条件数 {len(v.grade_table)} ≠ 13"


@pytest.mark.parametrize("issue,expect", [
    ("09120", "dlt-2007-05"), ("09121", "dlt-2009-10"),
    ("14051", "dlt-2009-10"), ("14052", "dlt-2014-05"),
    ("19018", "dlt-2014-05"), ("19019", "dlt-2019-02"),
    ("26013", "dlt-2019-02"), ("26014", "dlt-2026-02"),
])
def test_boundaries(issue, expect):
    """四个断点的**两侧**都必须落到正确版本。"""
    assert version_for_issue(issue).version == expect


def test_before_earliest_raises():
    with pytest.raises(RuleVersionError):
        version_for_key((2006, 150))


def test_2026_merges():
    """★ 2026 的两处合并：5+0 与 4+2 → 三等；4+0 与 3+2 → 五等。"""
    for h, j in ((5, 0), (4, 2)):
        assert classify_dlt(h, j, "dlt-2026-02") == "三等"
    for h, j in ((4, 0), (3, 2)):
        assert classify_dlt(h, j, "dlt-2026-02") == "五等"
    # 2019 版它们是分开的
    assert classify_dlt(5, 0, "dlt-2019-02") == "三等"
    assert classify_dlt(4, 2, "dlt-2019-02") == "四等"
    assert classify_dlt(4, 0, "dlt-2019-02") == "七等"
    assert classify_dlt(3, 2, "dlt-2019-02") == "六等"


def test_2014_third_is_merged_and_floating():
    """2014 段：三等 = 5+0 或 4+2，且**是浮动奖**。"""
    assert classify_dlt(5, 0, "dlt-2014-05") == "三等"
    assert classify_dlt(4, 2, "dlt-2014-05") == "三等"
    assert "三等" in version_by_name("dlt-2014-05").floating
    assert classify_dlt(4, 1, "dlt-2014-05") == "四等"
    assert classify_dlt(3, 2, "dlt-2014-05") == "四等"


def test_2007_eight_tiers():
    """2014 前 8 级：三等=5+0 且浮动；六=3+2/4+0；七=3+1/2+2；八=其余四条。"""
    v = version_by_name("dlt-2007-05")
    assert classify_dlt(5, 0, v.version) == "三等"
    assert "三等" in v.floating
    assert classify_dlt(4, 2, v.version) == "四等"
    assert classify_dlt(4, 1, v.version) == "五等"
    assert classify_dlt(3, 2, v.version) == "六等"
    assert classify_dlt(4, 0, v.version) == "六等"
    assert classify_dlt(3, 1, v.version) == "七等"
    assert classify_dlt(2, 2, v.version) == "七等"
    for hj in ((3, 0), (1, 2), (2, 1), (0, 2)):
        assert classify_dlt(*hj, v.version) == "八等"


def test_fixed_amounts_per_segment():
    assert fixed_amount("四等", "dlt-2007-05") == 3000
    assert fixed_amount("五等", "dlt-2007-05") == 500
    assert fixed_amount("六等", "dlt-2007-05") == 200
    assert fixed_amount("五等", "dlt-2009-10") == 600
    assert fixed_amount("六等", "dlt-2009-10") == 100
    assert fixed_amount("四等", "dlt-2014-05") == 200
    assert fixed_amount("三等", "dlt-2019-02") == 10000
    assert fixed_amount("九等", "dlt-2019-02") == 5
    assert fixed_amount("三等", "dlt-2026-02") == 5000
    # 浮动级没有固定金额
    assert fixed_amount("一等", "dlt-2026-02") is None


def test_uplift_only_2026_and_by_previous_pool():
    """8 亿上浮：只有 2026 版有，且按**上一期**奖池（开奖后余额）判定。"""
    assert version_by_name("dlt-2026-02").uplift["三等"] == 6666
    assert fixed_amount("三等", "dlt-2026-02", uplift=True) == 6666
    assert fixed_amount("七等", "dlt-2026-02", uplift=True) == 7
    assert uplift_applies("dlt-2026-02", 800_000_000) is True
    assert uplift_applies("dlt-2026-02", 799_999_999) is False
    assert uplift_applies("dlt-2026-02", None) is False
    # 其它版本没有该机制
    assert uplift_applies("dlt-2019-02", 9e8) is False


# ---------------------------------------------------------------- 概率金值（手算，不 import 生产）

def _p_dlt(h: int, j: int) -> float:
    """大乐透单注命中 (h 前区, j 后区) 的组合精确概率。"""
    front = math.comb(5, h) * math.comb(30, 5 - h)
    back = math.comb(2, j) * math.comb(10, 2 - j)
    return front * back / (math.comb(35, 5) * math.comb(12, 2))


def test_probability_goldens():
    """三个手算金值，供 C3 重构后比对（生产实现不能改这几个数）。"""
    assert _p_dlt(5, 2) * 21_425_712 == pytest.approx(1.0, rel=1e-12)
    # 2026 三等 = 5+0 或 4+2
    p3 = _p_dlt(5, 0) + _p_dlt(4, 2)
    assert p3 * 21_425_712 == pytest.approx(195.0, rel=1e-12)   # 9 + 186? 见下
    # 2026 七等 = 3+0 / 2+1 / 1+2 / 0+2
    p7 = _p_dlt(3, 0) + _p_dlt(2, 1) + _p_dlt(1, 2) + _p_dlt(0, 2)
    assert p7 * 21_425_712 == pytest.approx(
        math.comb(5, 3) * math.comb(30, 2) * math.comb(10, 2)
        + math.comb(5, 2) * math.comb(30, 3) * 2 * 10
        + math.comb(5, 1) * math.comb(30, 4) * 1
        + math.comb(5, 0) * math.comb(30, 5) * 1, rel=1e-12)


def test_issue_key_shared_implementation():
    """C0：data 与 ev 两侧必须是同一实现（同一行为）。"""
    from ev.profit.pit import issue_key as pit_key
    for s in ("3001", "9001", "10001", "26120", "2026120", "07001"):
        assert issue_key("双色球", s) == pit_key("双色球", s)
