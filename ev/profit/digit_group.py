# -*- coding: utf-8 -*-
"""ev/profit/digit_group.py —— 数字型「直选 / 组选3 / 组选6」的**概率层**（离线研究）。

为什么单独一个模块
------------------
`ev/profit/rules.py::LotteryRules.grade_table` 的二元格 `(h, j)` 里，`h` 是
**按位命中的位数**；数字型的 `outer_choose=False` 又决定了通用式分母是 `10^k`
（可重复排列）。组选玩法不在这套几何里：

  · 组选3 票面是「一个对子 + 一个独号」的**无序多重集**，中奖不要求位置对上；
  · 组选6 票面是「三个互异数字」的**无序集合**，中奖同样不要求位置对上；
  · 一张组选票覆盖 3 / 6 个**有序开奖结局**，而直选票只覆盖 1 个。

硬把组选塞进 `(h, j)` 格，会让 `ev/profit/evaluate.py`、`ev/tier_coverage.py`
这些按「命中位数」做直方图/枚举的下游拿到一个形状相同、含义全错的数 —— 不会报错，
属于「不会报错的错数」。所以本模块给出**独立、可复核的组合计数**，并由
`ev/payout.grade_probabilities(..., play=...)` 显式按玩法取用；不指定玩法时
一律还是直选（既有调用点零改动）。

组合计数（3 位数字型，0–9 可重复，开奖结局空间 = 10³ = 1000）
-------------------------------------------------------------
| 形态 | 开奖结局数 | 组选票面空间 | 每票覆盖结局数 | 中奖概率 | 单注奖金 |
|---|---|---|---|---|---|
| 直选（任意有序三位数） | 1000 | 1000 | 1 | 1/1000 | 1040 元 |
| 组选3（对子：aab） | 270 = 10×9×3 | 90 = 10×9 | 3 | 3/1000 | 346 元 |
| 组选6（全异：abc） | 720 = 10×9×8 | 120 = C(10,3) | 6 | 6/1000 | 173 元 |
| 豹子（三同：aaa） | 10 | — | — | 只有直选票能中 | 1040 元（直选） |

★ 豹子形态**没有组选票**：组选3 要求恰好一对，组选6 要求三个互异。
  10 + 270 + 720 = 1000，三种形态把开奖结局空间恰好分完；组选票面空间
  90×3=270、120×6=720 与对应形态的结局数一一相等（见 `validate_geometry()`）。

诚实边界
--------
· 生产票面是 **直选**；本模块是「如果改买组选，概率与 EV 是多少」的**反事实口径**，
  **不接生产选号**、不改注数、不写 pending、不占预算。
· 三种玩法返奖率同一水位（约 52%），换玩法不改变「无可证明优势（不参与）」的判决；
  建模的价值是把「按直选赔率算组选票」这个静默错数堵死，并让 EV 可以逐玩法复核。
· 未发现异常只表示已测维度在该样本量下未见异常，不构成证明。
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, Dict, Tuple

# 需要这套玩法的彩种（排列5 只有直选，不在此列）
DIGIT_GROUP_LOTTERIES: Tuple[str, ...] = ("排列3", "福彩3D")
PLAYS: Tuple[str, ...] = ("直选", "组选3", "组选6")

OUTCOME_SPACE = 1000                 # 10^3 个有序开奖结局
DIGITS = 10                         # 每位 0–9
WIDTH = 3

# 形态 → 开奖结局数（合计必须 = OUTCOME_SPACE）
SHAPE_COUNTS: Dict[str, int] = {
    "豹子": DIGITS,                                  # aaa：10
    "对子": DIGITS * (DIGITS - 1) * WIDTH,           # aab：10×9×3 = 270
    "全异": DIGITS * (DIGITS - 1) * (DIGITS - 2),    # abc：10×9×8 = 720
}

# 玩法 → 几何（票面空间 = 可买的不同注数；每票覆盖结局数 = 该票能赢的有序开奖数）
_GEOMETRY: Dict[str, Dict[str, Any]] = {
    "直选": {"票面空间": OUTCOME_SPACE, "每票覆盖结局数": 1,
             "形态": "全部", "奖金": 1040.0},
    "组选3": {"票面空间": DIGITS * (DIGITS - 1), "每票覆盖结局数": WIDTH,
              "形态": "对子", "奖金": 346.0},
    "组选6": {"票面空间": math.comb(DIGITS, WIDTH), "每票覆盖结局数": 6,
              "形态": "全异", "奖金": 173.0},
}

NOTE_NOT_PRODUCTION = (
    "生产票面为直选；组选3/组选6 的概率层是「若改玩法」的反事实口径，"
    "不接生产选号、不改注数、不占预算。"
)
NOTE_SHAPE = (
    "豹子（三同）形态没有组选票：组选3 要求恰好一对、组选6 要求三个互异；"
    "10 + 270 + 720 = 1000 把开奖结局空间恰好分完。"
)
NOTE_VERDICT = (
    "三种玩法返奖率同一水位（约 52%），换玩法不改变「无可证明优势（不参与）」的判决；"
    "未发现异常只表示已测维度在该样本量下未见异常，不构成证明。"
)


def shape_distribution() -> Dict[str, Dict[str, Any]]:
    """开奖形态分布。**分母 = 全部 1000 个有序结局**，不筛掉豹子后再报数。"""
    assert sum(SHAPE_COUNTS.values()) == OUTCOME_SPACE
    return {k: {"结局数": v, "概率": v / OUTCOME_SPACE,
                "占比": Fraction(v, OUTCOME_SPACE)}
            for k, v in SHAPE_COUNTS.items()}


def play_geometry(play: str) -> Dict[str, Any]:
    """取某玩法的组合几何；返回副本（调用方改动不污染常量）。未知玩法显式报错。"""
    if play not in _GEOMETRY:
        raise KeyError(f"数字型无此玩法：{play}（可选：{'/'.join(PLAYS)}）")
    g = dict(_GEOMETRY[play])
    g["玩法"] = play
    g["中奖概率"] = Fraction(g["每票覆盖结局数"], OUTCOME_SPACE)
    g["覆盖结局总数"] = g["票面空间"] * g["每票覆盖结局数"]
    return g


def win_probability(play: str) -> float:
    """单注中奖概率（float）。精确值请用 `play_geometry()['中奖概率']`（Fraction）。"""
    return float(play_geometry(play)["中奖概率"])


def grade_probabilities(play: str) -> Dict[str, float]:
    """按 `ev/payout.grade_probabilities` 的约定返回**只有中奖奖级**的字典。

    不写「未中」：下游 `for grade, p in probs.items()` 会拿奖级名去查奖金表，
    多一个「未中」键会让每条 EV 链都在 `base_payout` 里静默取到 0。
    """
    if play not in PLAYS:
        raise KeyError(f"数字型无此玩法：{play}（可选：{'/'.join(PLAYS)}）")
    return {play: win_probability(play)}


def validate_geometry() -> Dict[str, Any]:
    """几何自洽对账（可复核，不做判决）。任何一条不成立都是静默错数的入口。"""
    shapes = SHAPE_COUNTS
    checks = {
        "形态合计等于结局空间": sum(shapes.values()) == OUTCOME_SPACE,
        "组选3覆盖等于对子结局数": (play_geometry("组选3")["覆盖结局总数"]
                                    == shapes["对子"]),
        "组选6覆盖等于全异结局数": (play_geometry("组选6")["覆盖结局总数"]
                                    == shapes["全异"]),
        "直选覆盖等于全部结局数": (play_geometry("直选")["覆盖结局总数"]
                                    == OUTCOME_SPACE),
    }
    return {"逐条": checks, "全部成立": all(checks.values()),
            "开奖结局空间": OUTCOME_SPACE, "形态": dict(shapes)}


def ev_table(lottery: str, *, cost: float = 2.0) -> Dict[str, Any]:
    """逐玩法的概率 / 期望回款 / 净 EV / 返奖率（**离线反事实**，不接生产）。

    奖金取 `data.prize_table.FIXED_PAYOUT` 单一真源，不在本模块手抄第二份数值；
    `cost` 缺省 2 元，可由调用方按 `config.ticket_price()` 注入。
    """
    if lottery not in DIGIT_GROUP_LOTTERIES:
        raise KeyError(f"{lottery} 没有组选玩法（仅 {DIGIT_GROUP_LOTTERIES}）")
    from data.prize_table import FIXED_PAYOUT

    pay = FIXED_PAYOUT[lottery]
    rows = []
    for play in PLAYS:
        g = play_geometry(play)
        amt = float(pay[play])
        p = win_probability(play)
        back = amt * p
        rows.append({
            "玩法": play, "奖金元": amt, "中奖概率": p,
            "中奖概率精确": str(g["中奖概率"]),
            "票面空间": g["票面空间"], "每票覆盖结局数": g["每票覆盖结局数"],
            "覆盖形态": g["形态"],
            "期望回款元": round(back, 6), "净EV元": round(back - cost, 6),
            "返奖率": back / cost if cost else None,
            "备注": ("豹子形态无组选票，仅直选覆盖" if play != "直选" else ""),
        })
    return {
        "彩种": lottery, "单注成本元": cost, "玩法": rows,
        "几何自检": validate_geometry(),
        "口径": [NOTE_NOT_PRODUCTION, NOTE_SHAPE, NOTE_VERDICT],
        "奖金来源": "data.prize_table.FIXED_PAYOUT",
        "不接生产": True,
    }


__all__ = [
    "DIGIT_GROUP_LOTTERIES", "DIGITS", "NOTE_NOT_PRODUCTION", "NOTE_SHAPE",
    "NOTE_VERDICT", "OUTCOME_SPACE", "PLAYS", "SHAPE_COUNTS", "WIDTH",
    "ev_table", "grade_probabilities", "play_geometry",
    "shape_distribution", "validate_geometry", "win_probability",
]
