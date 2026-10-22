# -*- coding: utf-8 -*-
"""ev/profit/rules.py —— 六彩种的奖金规则表与「命中格 → 奖级」映射。

设计要点
--------
1. **统一结构**：六个彩种都能写成同一个二元格 `(外层命中数 h, 内层命中类 j)`：
       · 双色球   外层=红球(33选6)，内层=蓝球(16)
       · 大乐透   外层=前区(35选5)，内层=后区命中数 j∈{0,1,2}
       · 七星彩   外层=前 6 位，    内层=第 7 位是否命中（第 7 位 0-14，共 15 值）
       · 排列5/3/D 外层=全部位数，  内层=空（单一奖级）
   于是「枚举 + 命中直方图」的算法对六彩种**同一套代码**，只是格表不同。

2. **七星彩的奖级规则照抄 `data/feedback._qxc_prize`**（现行版 2020 升级）：
   设 X=前 6 位命中数、Y=第 7 位是否命中、T=X+Y，
       T=7 → 一等 | X=6,Y=0 → 二等 | X=5,Y=1 → 三等 | T=5 → 四等
       T=4 → 五等 | T≥3 或 Y=1 → 六等 | 其余未中
   官方中奖注数 1/14/54/1971/31590/1188270（分母 1500 万）可由此格表逐条复算，
   本模块的测试会锁这条一致性。

3. **浮动奖级**只用 `FLOATING_REF` 的封顶值；基准池由 `payouts.py` 按当期可得信息推。
   `FLOATING_REF[*]["二等"]` 为 None 表示「无独立封顶」，不是「不浮动」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from data.prize_table import FIXED_PAYOUT, FLOATING_REF

# 单注成本（元）。大乐透若买追加投注则为 3 元，本模型只做基本投注。
COST = 2.0

# 大乐透追加投注反解占比 q（由票量校准因子 1.1324 反推：均价 = 2×1.1324 = 2.2648，
# 而均价 = 2×(1−q) + 3×q ⇒ q = 0.2648）。实际票量 N = 销售额 / 均价。
DLT_ADDON_SHARE = 0.2648
DLT_AVG_PRICE = 2.0 * (1 - DLT_ADDON_SHARE) + 3.0 * DLT_ADDON_SHARE

# 双色球票量校准因子实测 1.0041（CI 含 1）⇒ N = 销售额 / 2 成立
SSQ_TICKET_FACTOR = 1.0041


@dataclass(frozen=True)
class GradeRule:
    """单个奖级的规则。"""
    name: str
    kind: str                     # "fixed" | "floating"
    fixed: Optional[float] = None  # 固定奖单注金额
    cap: Optional[float] = None    # 单注封顶（floating 才有）
    prize_level: Optional[int] = None  # market.db.prize.level（取当期注数用）
    risk_control: bool = False     # 受「风险控制」下调（排列5 直选）


@dataclass(frozen=True)
class LotteryRules:
    """一个彩种的完整规则。"""
    name: str
    cost: float
    outer_label: str               # "红球" / "前区" / "前6位" / "全部位数"
    outer_width: int               # 外层位数（选几个 / 几位）
    outer_range: Tuple[int, int]   # 外层取值范围（含端点）
    outer_choose: bool             # True=组合(不计顺序)，False=排列(计顺序)
    inner_size: int                # 内层状态数（蓝球 16 / 后区组合 66 / 第7位 15 / 1）
    inner_multiplicity: Dict[int, int]  # 内层命中类 j → 该类的状态个数
    grade_table: Dict[Tuple[int, int], str]  # (h, j) → 奖级名
    grades: Dict[str, GradeRule]
    rule_version: str
    effective_from: str
    source: str
    avg_ticket_price: float = COST

    # ---- 结局空间 ----
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
        """(外层命中数, 内层命中类) → 奖级名；未中返回 None。"""
        return self.grade_table.get((h, j))

    def floating(self) -> Tuple[str, ...]:
        return tuple(g for g, r in self.grades.items() if r.kind == "floating")


# ---------------------------------------------------------------- 格表

_SSQ_TABLE = {
    (6, 1): "一等", (6, 0): "二等",
    (5, 1): "三等", (5, 0): "四等", (4, 1): "四等",
    (4, 0): "五等", (3, 1): "五等",
    (2, 1): "六等", (1, 1): "六等", (0, 1): "六等",
}

_DLT_TABLE = {
    (5, 2): "一等", (5, 1): "二等", (5, 0): "三等",
    (4, 2): "四等", (4, 1): "五等", (3, 2): "六等",
    (4, 0): "七等", (3, 1): "八等", (2, 2): "八等",
    (3, 0): "九等", (2, 1): "九等", (1, 2): "九等", (0, 2): "九等",
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
    """由 prize_table 的固定奖表 + 浮动奖表构造 GradeRule 字典。"""
    out: Dict[str, GradeRule] = {}
    fixed = FIXED_PAYOUT.get(name, {})
    float_ref = FLOATING_REF.get(name, {})
    level_map = {
        "双色球": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6},
        "大乐透": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5,
                   "六等": 6, "七等": 7, "八等": 8, "九等": 9},
        "七星彩": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6},
        "排列3": {"直选": 1},
        "福彩3D": {"直选": 1},
        "排列5": {"直选": 1},
    }.get(name, {})

    for g, amt in fixed.items():
        out[g] = GradeRule(name=g, kind="fixed", fixed=float(amt),
                           prize_level=level_map.get(g))
    for g, cap in float_ref.items():
        out[g] = GradeRule(name=g, kind="floating", cap=float(cap) if cap else None,
                           prize_level=level_map.get(g))
    # 排列5 直选名义 10 万但受风险控制（实测 0.36% 的期被下调）
    if name == "排列5":
        r = out.get("直选")
        if r is not None:
            out["直选"] = GradeRule(name="直选", kind="fixed", fixed=r.fixed,
                                    prize_level=1, risk_control=True)
    return out


def _rules_ssq() -> LotteryRules:
    return LotteryRules(
        name="双色球", cost=COST, outer_label="红球", outer_width=6,
        outer_range=(1, 33), outer_choose=True,
        inner_size=16, inner_multiplicity={1: 1, 0: 15},
        grade_table=_SSQ_TABLE, grades=_build_grades("双色球"),
        rule_version="ssq-2026-02", effective_from="2026-02-01",
        source="《中国福利彩票双色球游戏规则》（2026-02 起含福运奖，本模型只建六大奖级）",
        avg_ticket_price=2.0,
    )


def _rules_dlt() -> LotteryRules:
    return LotteryRules(
        name="大乐透", cost=COST, outer_label="前区", outer_width=5,
        outer_range=(1, 35), outer_choose=True,
        inner_size=66, inner_multiplicity={2: 1, 1: 20, 0: 45},
        grade_table=_DLT_TABLE, grades=_build_grades("大乐透"),
        rule_version="dlt-2026-02", effective_from="2026-02-01",
        source="《超级大乐透游戏规则》（2026-02 起取消 8/9 等奖，本表为 2019-02 版九级）",
        avg_ticket_price=DLT_AVG_PRICE,
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


_RULES: Dict[str, LotteryRules] = {
    "双色球": _rules_ssq(),
    "大乐透": _rules_dlt(),
    "七星彩": _rules_qxc(),
    "排列3": _rules_digit("排列3", 3),
    "福彩3D": _rules_digit("福彩3D", 3),
    "排列5": _rules_digit("排列5", 5),
}

SUPPORTED = tuple(_RULES)


def rules_for(lottery: str) -> LotteryRules:
    """取彩种规则。未知彩种直接抛，不静默降级。"""
    if lottery not in _RULES:
        raise KeyError(f"盈利模型不支持的彩种: {lottery}（支持 {SUPPORTED}）")
    return _RULES[lottery]


def outcome_size(lottery: str) -> int:
    return rules_for(lottery).outcome_size


def avg_ticket_price(lottery: str) -> float:
    return rules_for(lottery).avg_ticket_price
