# -*- coding: utf-8 -*-
"""ev/profit/amounts.py —— 单注奖金的**唯一取值链**。

三类奖级、三种取法（2026-11-01 因七乐彩/快乐8 接入而显式化）：

    kind="fixed"      金额恒定，直接取 GradeRule.fixed
    kind="floating"   奖池按注均分，必须靠当期实测 / float_pool 建模（本模块不猜）
    kind="regulated"  名义值由规则规定、**单注实际金额按期由官方公布**，已含
                      分摊/封顶/限赔/派奖 ⇒ 当期实测优先，查不到才回退名义并标注

★ 为什么 regulated 不用"名义值 + 分薄模型"：
  官方公布的单注金额**已经**算过分摊与限赔了。再乘一次 E[1/(1+X)] 是重复打折，
  会把返奖率系统性地算低。这一点与 ssq/dlt 的浮动奖相反（那两档官方只公布
  总奖金池，单注金额确实要自己分）。
"""
from __future__ import annotations

from typing import Optional, Tuple

# 来源标注：调用方**必须**把这个带到界面/账本上，不允许只给金额。
SRC_ACTUAL = "实测"
SRC_ACTUAL_AWARD = "实测+派奖"
SRC_NOMINAL = "名义参考"
SRC_FIXED = "固定奖"
SRC_FLOATING_UNKNOWN = "未知（浮动奖无当期数据）"
SRC_NO_GRADE = "未知奖级"


def grade_amount(lottery: str, issue, grade: str, *,
                 play: Optional[str] = None) -> Tuple[Optional[float], str]:
    """返回 (单注金额 or None, 来源)。

    None **不等于 0**：它表示"这张票中了，但这一注值多少钱现在还不知道"。
    调用方必须把它排除出求和并单独计数（与账本同一纪律），
    不能当 0 计入（偏低）也不能按上限虚报。
    """
    from data.prize_table import norm_grade
    from ev.profit.rules import PLAYS, rules_for

    r = rules_for(lottery, play=play)
    key = norm_grade(grade)
    g = r.grades.get(key)
    if g is None:
        # ★ 快乐8 的奖级名自带玩法（"选九中九" ⇒ 玩法"选九"）。
        #   调用方没传 play 时默认是选十，直接查会落到「未知奖级」并静默返回 None
        #   —— 而 None 在这条链路里的含义是"中了但金额未知"，会被混进"待定"计数。
        #   所以这里按名字反推玩法，而不是让调用方处处记得传 play。
        for p in PLAYS.get(lottery, ()):
            if p and key.startswith(f"{p}中"):
                r = rules_for(lottery, play=p)
                g = r.grades.get(key)
                break
    if g is None:
        return None, SRC_NO_GRADE

    if g.kind == "fixed":
        return (float(g.fixed) if g.fixed is not None else None), SRC_FIXED

    from data.prize_amounts import actual_per_bet

    amt, src = actual_per_bet(lottery, issue, key)
    if amt is not None:
        return float(amt), src

    if g.kind == "regulated" and g.nominal is not None:
        return float(g.nominal), SRC_NOMINAL
    return None, SRC_FLOATING_UNKNOWN


def grade_amount_map(lottery: str, issue, grades, *,
                     play: Optional[str] = None) -> dict:
    """一次取一期的多档金额：{奖级: (金额, 来源)}。"""
    return {g: grade_amount(lottery, issue, g, play=play) for g in grades}


__all__ = [
    "SRC_ACTUAL", "SRC_ACTUAL_AWARD", "SRC_FIXED", "SRC_FLOATING_UNKNOWN",
    "SRC_NO_GRADE", "SRC_NOMINAL", "grade_amount", "grade_amount_map",
]
