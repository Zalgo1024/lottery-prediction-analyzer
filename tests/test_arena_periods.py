# -*- coding: utf-8 -*-
"""tests/test_arena_periods.py —— 期窗是不变量：不许求交、取众数、截断。

★ 2026-11-06 判据变更（重要，勿回退）
------------------------------------
原 `test_full_window_matches_known_size` 硬编码 `== 3523`（双色球全量期数），
每夜 23:10 开奖入库就撞红。历史做法是「bump 钉子 + 同提交 CSV」（如 commit
`a6b457a`），但本项目 `lottery_data/*.csv` 是**流水线实时写入的生产状态文件**、
按红线不得提交；单独 bump 会让干净 checkout 的 CI 变红 ⇒ 该钉在本仓库
**不可维护**，每次全量都带一个「不是 bug 的失败」，反而麻痹对真失败的警觉。

改为「与数据文件逐位一致」的不变量：
  · 基准 = `_csv_issues()` 独立解析数据文件的期号列（**不经过**
    `data.loader` / `ev.tier_backtest`，只看文件自己说了什么）；
  · 被测 = `ev.arena.periods.build_periods()`。
两者同源同文件 ⇒ CI 与本地恒绿；而窗口构造的求交 / 截断 / 丢期 / 重复 /
乱序仍会红（负向用例 `test_window_diff_*` 逐项证明牙齿还在）。

保留的边界：`MIN_PERIODS` 是**粗下限**，防数据文件被误传成空/极少行时
出现「0 == 0」式假通过。它不是期数钉，不需要随开奖 bump。
"""
from __future__ import annotations

from collections import Counter

import pytest

from ev.arena import periods as PD

# 8 彩种全覆盖（★ 不是只钉双色球：窗口构造是共用代码，单彩种通过不代表其余通过）
LOTS = ("双色球", "大乐透", "排列3", "排列5", "七星彩", "福彩3D", "七乐彩", "快乐8")

# 粗下限：仅用于「数据文件被截断成空/极少行」时报警，不随开奖 bump。
MIN_PERIODS = 1000


def _csv_issues(lottery: str) -> list:
    """独立解析数据文件「期号」列，返回**升序 int 列表**。

    ★ 刻意不经过 `data.loader` / `ev.tier_backtest`：本函数的角色是「文件说了
      什么」，被测的是「`build_periods` 构造出什么」，两者必须来自不同代码路径，
      否则就是自己跟自己比。只用 `_resolve_csv_path()` 定位「loader 实际会读的
      那个文件」（双色球走 `_cleaned`，其余走原始 CSV）。
    """
    from data.loader import _resolve_csv_path

    path = _resolve_csv_path(lottery)
    text = path.read_bytes().decode("gbk", errors="replace")
    out = []
    for i, line in enumerate(text.splitlines()):
        if not line.strip() or i == 0:          # 空行 / 表头
            continue
        cell = line.split(",")[0].strip()
        if not cell.lstrip("-").isdigit():
            raise AssertionError(f"{path.name} 第 {i + 1} 行期号非数字：{cell!r}")
        out.append(int(cell))
    return sorted(out)


def _window_diff(bp: list, csv_iss: list) -> str:
    """返回差异描述（空串=一致）。抽成纯函数，便于对丢期/重复/乱序做负向测试。"""
    if bp == csv_iss:
        return ""
    missing = sorted(set(csv_iss) - set(bp))
    extra = sorted(set(bp) - set(csv_iss))
    dup = sorted(k for k, v in Counter(bp).items() if v > 1)
    parts = []
    if missing:
        parts.append(f"丢期 {len(missing)} 个（例 {missing[:5]}）")
    if extra:
        parts.append(f"多出 {len(extra)} 个（例 {extra[:5]}）")
    if dup:
        parts.append(f"重复 {len(dup)} 个（例 {dup[:5]}）")
    if not parts:
        parts.append(f"顺序不一致（长度 {len(bp)} vs {len(csv_iss)}）")
    return "；".join(parts)


@pytest.mark.parametrize("lot", LOTS)
def test_window_matches_data_file(lot):
    """全量期窗必须与数据文件逐位一致（防求交 / 截断 / 丢期 / 重复 / 乱序）。"""
    csv_iss = _csv_issues(lot)
    assert len(csv_iss) >= MIN_PERIODS, (
        f"{lot} 数据文件只有 {len(csv_iss)} 期 < 粗下限 {MIN_PERIODS}，疑似被截断")
    bp = [int(x) for x in PD.build_periods(lot)]
    assert _window_diff(bp, csv_iss) == "", f"{lot} 期窗与数据文件不一致"


def test_build_periods_ascending_numeric():
    iss = PD.build_periods("双色球", 200)
    assert len(iss) == 200
    # ★ 期号按**数值**升序（2003~2009 是一位年 3001…，跨年重编号 26281→27001）
    nums = [int(x) for x in iss]
    assert nums == sorted(nums)
    assert nums[-1] > nums[0]


def test_build_periods_tail_is_tail_of_full_window():
    """`periods=N` 必须是全量窗口的**末 N 期**，不是头部、也不是重采样。"""
    full = [int(x) for x in PD.build_periods("双色球")]
    tail = [int(x) for x in PD.build_periods("双色球", 300)]
    assert tail == full[-300:]


# ---------------- 负向用例：证明上面的不变量仍有牙齿 ----------------
def test_window_diff_ok_on_equal():
    assert _window_diff([1, 2, 3], [1, 2, 3]) == ""


def test_window_diff_detects_dropped():
    assert "丢期" in _window_diff([1, 3], [1, 2, 3])


def test_window_diff_detects_extra():
    assert "多出" in _window_diff([1, 2, 3, 4], [1, 2, 3])


def test_window_diff_detects_duplicate():
    assert "重复" in _window_diff([1, 2, 2, 3], [1, 2, 3])


def test_window_diff_detects_reorder():
    """求交/重排会改变顺序 —— 必须能抓到，不能只看集合相等。"""
    assert _window_diff([2, 1, 3], [1, 2, 3]) != ""


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
