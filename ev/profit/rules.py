# -*- coding: utf-8 -*-
"""ev/profit/rules.py —— 彩种奖金规则表与「命中格 → 奖级」映射（**版本化**）。

设计要点
--------
1. **统一结构**：六个彩种都能写成同一个二元格 `(外层命中数 h, 内层命中类 j)`：
       · 双色球   外层=红球(33选6)，内层=蓝球(16)
       · 大乐透   外层=前区(35选5)，内层=后区命中数 j∈{0,1,2}
       · 七星彩   外层=前 6 位，    内层=第 7 位是否命中（第 7 位 0-14，共 15 值）
       · 排列5/3/D 外层=全部位数，  内层=空（单一奖级）
   于是「枚举 + 命中直方图」的算法对六彩种**同一套代码**，只是格表不同。

2. **大乐透有五个规则版本**（见 `data/prize_versions.py`），`rules_for(lottery, issue)`
   按期号选版本；`issue=None` 一律返回**现行版本**，因此现有「只传彩种」的调用点零改动。

3. `grade_probabilities` 等下游必须用 `ctx.rules.rule_version` 取对应版本，
   否则 2026 段会拿九级概率算七级奖（EV 偏约 0.1 元/注）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from data.prize_versions import (
    DLT_VERSIONS,
    DltVersion,
    RuleVersionError,
    version_for_issue,
)

# 单注成本（元）。大乐透若买追加投注则为 3 元，本模型只做基本投注。
COST = 2.0

# 大乐透追加投注反解占比 q（由票量校准因子 1.1324 反推：均价 = 2×1.1324 = 2.2648，
# 而均价 = 2×(1−q) + 3×q ⇒ q = 0.2648）。实际票量 N = 销售额 / 均价。
DLT_ADDON_SHARE = 0.2648
DLT_AVG_PRICE = 2.0 * (1 - DLT_ADDON_SHARE) + 3.0 * DLT_ADDON_SHARE

# 双色球票量校准因子实测 1.0041（CI 含 1）⇒ N = 销售额 / 2 成立
SSQ_TICKET_FACTOR = 1.0041

CAP_DEFAULT = 10_000_000.0


@dataclass(frozen=True)
class GradeRule:
    """单个奖级的规则。"""
    name: str
    kind: str                     # "fixed" | "floating"
    fixed: Optional[float] = None  # 固定奖单注金额
    cap: Optional[float] = None    # 单注封顶（floating 才有）
    prize_level: Optional[int] = None
    risk_control: bool = False


@dataclass(frozen=True)
class LotteryRules:
    name: str
    cost: float
    outer_label: str
    outer_width: int
    outer_range: Tuple[int, int]
    outer_choose: bool
    inner_size: int
    inner_multiplicity: Dict[int, int]
    grade_table: Dict[Tuple[int, int], str]
    grades: Dict[str, GradeRule]
    rule_version: str
    effective_from: str
    source: str
    avg_ticket_price: float = COST
    until_issue: Optional[str] = None      # ★ 带默认值 ⇒ 不破坏现有构造

    @property
    def outer_size(self) -> int:
        lo, hi = self.outer_range
        n = hi - lo + 1
        if self.outer_choose:
            return math.comb(n, self.outer_width)
        return n ** self.outer_width

    @property
    def outcome_size(self) -> int:
        return self.outer_size * self.inner_size

    def grade_of(self, h: int, j: int) -> Optional[str]:
        return self.grade_table.get((h, j))

    def floating(self) -> Tuple[str, ...]:
        return tuple(g for g, r in self.grades.items() if r.kind == "floating")


# ---------------------------------------------------------------- 非大乐透的格表

_SSQ_TABLE = {
    (6, 1): "一等", (6, 0): "二等",
    (5, 1): "三等", (5, 0): "四等", (4, 1): "四等",
    (4, 0): "五等", (3, 1): "五等",
    (2, 1): "六等", (1, 1): "六等", (0, 1): "六等",
}


def _qxc_table() -> Dict[Tuple[int, int], str]:
    """七星彩：X=前6位命中数、Y=第7位是否命中（照 data.feedback._qxc_prize）。"""
    t: Dict[Tuple[int, int], str] = {}
    for x in range(0, 7):
        for y in (0, 1):
            if x + y == 7:
                g = "一等"
            elif x == 6 and y == 0:
                g = "二等"
            elif x == 5 and y == 1:
                g = "三等"
            elif x + y == 5:
                g = "四等"
            elif x + y == 4:
                g = "五等"
            elif x + y >= 3 or y == 1:
                g = "六等"
            else:
                continue
            t[(x, y)] = g
    return t


def _build_grades(name: str) -> Dict[str, GradeRule]:
    """非大乐透彩种：由 prize_table 的固定/浮动奖表构造（**惰性导入**避免循环依赖）。"""
    from data.prize_table import FIXED_PAYOUT, FLOATING_REF

    out: Dict[str, GradeRule] = {}
    level_map = {
        "双色球": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6},
        "七星彩": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6},
        "排列3": {"直选": 1}, "福彩3D": {"直选": 1}, "排列5": {"直选": 1},
    }.get(name, {})
    for g, amt in FIXED_PAYOUT.get(name, {}).items():
        out[g] = GradeRule(name=g, kind="fixed", fixed=float(amt),
                           prize_level=level_map.get(g))
    for g, cap in FLOATING_REF.get(name, {}).items():
        out[g] = GradeRule(name=g, kind="floating",
                           cap=float(cap) if cap else None,
                           prize_level=level_map.get(g))
    if name == "排列5" and "直选" in out:
        r = out["直选"]
        out["直选"] = GradeRule(name="直选", kind="fixed", fixed=r.fixed,
                                prize_level=1, risk_control=True)
    return out


def _grades_dlt(v: DltVersion) -> Dict[str, GradeRule]:
    """大乐透：**完全由版本表生成**（不再手写九级，也不再读 prize_table）。"""
    order = ["一等", "二等", "三等", "四等", "五等", "六等", "七等", "八等", "九等"]
    out: Dict[str, GradeRule] = {}
    for g in sorted(set(v.grade_table.values()), key=order.index):
        if g in v.floating:
            cap = v.first_cap if g == "一等" else None
            out[g] = GradeRule(name=g, kind="floating", cap=cap, prize_level=order.index(g) + 1)
        else:
            out[g] = GradeRule(name=g, kind="fixed", fixed=float(v.fixed[g]),
                               prize_level=order.index(g) + 1)
    return out


def _rules_ssq() -> LotteryRules:
    return LotteryRules(
        name="双色球", cost=COST, outer_label="红球", outer_width=6,
        outer_range=(1, 33), outer_choose=True,
        inner_size=16, inner_multiplicity={1: 1, 0: 15},
        grade_table=dict(_SSQ_TABLE), grades=_build_grades("双色球"),
        rule_version="ssq-2026-02", effective_from="2026-02-01",
        source="《中国福利彩票双色球游戏规则》（本模型只建六大奖级）",
        avg_ticket_price=2.0,
    )


def _rules_dlt(v: DltVersion) -> LotteryRules:
    return LotteryRules(
        name="大乐透", cost=COST, outer_label="前区", outer_width=5,
        outer_range=(1, 35), outer_choose=True,
        inner_size=66, inner_multiplicity={2: 1, 1: 20, 0: 45},
        grade_table=dict(v.grade_table), grades=_grades_dlt(v),
        rule_version=v.version, effective_from=v.from_date,
        source=v.source, avg_ticket_price=DLT_AVG_PRICE,
    )


def _rules_qxc() -> LotteryRules:
    return LotteryRules(
        name="七星彩", cost=COST, outer_label="前6位", outer_width=6,
        outer_range=(0, 9), outer_choose=False,
        inner_size=15, inner_multiplicity={1: 1, 0: 14},
        grade_table=_qxc_table(), grades=_build_grades("七星彩"),
        rule_version="qxc-2020", effective_from="2020-10-11",
        source="《七星彩游戏规则》2020 升级版（任意位置匹配位数，不要求连续）",
        avg_ticket_price=2.0,
    )


def _rules_digit(name: str, width: int) -> LotteryRules:
    return LotteryRules(
        name=name, cost=COST, outer_label="全部位数", outer_width=width,
        outer_range=(0, 9), outer_choose=False,
        inner_size=1, inner_multiplicity={0: 1},
        grade_table={(width, 0): "直选"}, grades=_build_grades(name),
        rule_version=f"{name}-fixed", effective_from="2004-01-01",
        source="固定赔率玩法（直选），不涉及奖池与分摊",
        avg_ticket_price=2.0,
    )


# 彩种 → **升序**版本元组。大乐透五段，其余单版本。
_RULES: Dict[str, Tuple[LotteryRules, ...]] = {
    "双色球": (_rules_ssq(),),
    "大乐透": tuple(_rules_dlt(v) for v in DLT_VERSIONS),
    "七星彩": (_rules_qxc(),),
    "排列3": (_rules_digit("排列3", 3),),
    "福彩3D": (_rules_digit("福彩3D", 3),),
    "排列5": (_rules_digit("排列5", 5),),
}

# 大乐透版本 → 起始 (年, 序号)
_DLT_SERIES: Tuple[Tuple[Tuple[int, int], LotteryRules], ...] = tuple(
    (v.from_key, r) for v, r in zip(DLT_VERSIONS, _RULES["大乐透"])
)

# ★ 版本只改奖级，不改几何：枚举缓存（outcomes.outer_universe 的 lru_cache(key=lottery)）因此安全
assert len({r.outer_size for r in _RULES["大乐透"]}) == 1

SUPPORTED = tuple(_RULES)


def rule_versions(lottery: str) -> Tuple[str, ...]:
    return tuple(r.rule_version for r in _RULES[lottery])


# 「X等」奖级的级数序（七星彩用「X等奖」另一套命名，不在此表）
_GRADE_ORDER = ("一等", "二等", "三等", "四等", "五等", "六等", "七等", "八等", "九等")


def grade_rank_names() -> Tuple[str, ...]:
    """「X等」奖级名全集（按级数升序）—— 供展示层排序/筛选权重使用。

    大乐透历史上有 6/7/8/9 级四种级数（双色球/七星彩 6 级），展示层不再手写
    「一~九等」副本：从本表取，缺级天然不存在（`.get(x, 99)` 语义不变）。
    数字型的「直选/组选3/组选6」不在此表（由调用方自行加权）。
    """
    names = {g for lottery in SUPPORTED for r in _RULES[lottery] for g in r.grades
             if g.endswith("等")}
    assert names <= set(_GRADE_ORDER), f"未知奖级名: {names - set(_GRADE_ORDER)}"
    return tuple(sorted(names, key=_GRADE_ORDER.index))


def rules_for(lottery: str, issue: Any = None) -> LotteryRules:
    """取彩种规则。

    `issue=None` ⇒ **现行版本**（决策路径，现有调用点行为不变）。
    给定 `issue` ⇒ 按 (年, 序号) 从新到旧取第一个 `key >= from_key`；
    早于最早版本抛 `RuleVersionError`，**不静默降级**。
    """
    vs = _RULES[lottery]
    if issue is None:
        return vs[-1]
    from data.issue_key import issue_key

    key = issue_key(lottery, issue)
    for r in reversed(vs):
        if key >= _series_key(lottery, r.rule_version):
            return r
    raise RuleVersionError(
        f"{lottery} 期号 {issue} 早于最早规则版本 {rule_versions(lottery)[0]}")


def rules_by_version(lottery: str, version: str) -> LotteryRules:
    """按版本名取规则（供 payout.grade_probabilities 用）。未知版本抛 KeyError。"""
    for r in _RULES[lottery]:
        if r.rule_version == version:
            return r
    raise KeyError(f"{lottery} 没有规则版本 {version}（有 {rule_versions(lottery)}）")


def _series_key(lottery: str, version: str) -> Tuple[int, int]:
    if lottery == "大乐透":
        for k, r in _DLT_SERIES:
            if r.rule_version == version:
                return k
    return (0, 0)


def outcome_size(lottery: str) -> int:
    return rules_for(lottery).outcome_size


def avg_ticket_price(lottery: str) -> float:
    return rules_for(lottery).avg_ticket_price


__all__ = [
    "CAP_DEFAULT", "COST", "DLT_ADDON_SHARE", "DLT_AVG_PRICE", "GradeRule",
    "LotteryRules", "RuleVersionError", "SUPPORTED", "avg_ticket_price",
    "grade_rank_names", "outcome_size", "rule_versions", "rules_by_version",
    "rules_for",
]
