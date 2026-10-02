"""
ev/stake.py —— 效用/注数决策（策略升级 C）

回答「买几注」。期望线性性下注数不改变单位 EV，但强烈改变方差/分布形状——
这不是 EV 问题，是风险偏好（效用）问题。

框架（诚实定位）：
- Kelly 纪律（ev/kelly.py）：f* = (b·p − q)/b，EV≤0 ⇔ f*≤0 → 0 注（数学结论，非建议）。
  本项目所有彩种单注名义 EV 均为负 → 正确输出通常为 0；只有 rollover/限号修正
  EV > 0 的窗口（见 ev/rollover.py 参与信号）才可能 > 0。
- 预算纪律（ev/bankroll.py）：单期预算 = 本金 2%、止损/熔断优先于一切信号。
- 实际建议注数 = min(Kelly 理论注数, 预算注数, 单期上限)。

诚实边界：Kelly 假设同分布可无限细分重复下注；彩票单期离散、不可微注，
结果为理论上限展示。偏度/峰度刻画（收益分布是「频繁小赢」还是「重尾大赢」）
见反事实回测收益序列，重尾 → 更应用 1/4 Kelly 而非半 Kelly。
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HONEST_NOTE = (
    "Kelly 假设同分布可无限细分重复下注；彩票单期离散、不可微注，结果为理论上限展示；"
    "所有彩种单注名义 EV 为负 → 正确输出通常为 0（纪律不下注）。"
    "偏度/峰度：收益重尾时建议 1/4 Kelly 替代半 Kelly。"
)


def stake_plan(ev_per_ticket: float, prob: float, cost: float = 2.0,
               bankroll: float = 1000.0, frac: float = 0.5,
               budget: Optional[Any] = None,
               cap_tickets: Optional[int] = None,
               consec_loss: int = 0, cumulative_pnl: float = 0.0) -> Dict[str, Any]:
    """
    注数决策。

    ev_per_ticket: 单注期望盈亏（元；净口径，含成本）。
    prob:          单注中奖概率（用于 Kelly 的赔率换算）。
    frac:          Kelly 缩水（0.5=半 Kelly，0.25=1/4 Kelly；重尾收益建议 0.25）。
    cap_tickets:   外部单期上限（如限号可购量）。

    返回 {单注EV, kelly比例, 理论注数, 预算注数, 实际建议注数, 触发约束, 诚实边界}。
    """
    from ev.bankroll import Budget, budget_plan
    from ev.kelly import kelly_from_ev

    f = kelly_from_ev(ev_per_ticket, cost, prob, frac=frac)
    bp = budget_plan(budget or Budget(bankroll=bankroll),
                     consec_loss=consec_loss, cumulative_pnl=cumulative_pnl)
    n_budget = int(bp["单期最大注数"])

    constraints = []
    if ev_per_ticket <= 0:
        n_theory = 0
        constraints.append("EV≤0 → Kelly=0（数学结论，纪律不下注）")
    elif bp.get("状态") != "正常":
        n_theory = 0
        constraints.append(f"资金纪律优先：{bp['建议']}")
    else:
        n_theory = int(f * bankroll // cost)
        if f > 0 and n_theory == 0:
            n_theory = 1 if f * bankroll >= cost * 0.5 else 0
            if n_theory == 0:
                constraints.append("Kelly 注数 < 0.5 注（资金过小），按 0 处理")

    n_final = min(n_theory, n_budget) if n_theory else 0
    if cap_tickets is not None and n_final > cap_tickets:
        n_final = cap_tickets
        constraints.append(f"外部单期上限 {cap_tickets} 生效")
    if n_budget < n_theory and n_theory > 0:
        constraints.append(f"预算约束生效（预算 {n_budget} 注 < Kelly 理论 {n_theory} 注）")
    if not constraints:
        constraints.append("无约束触发（Kelly 理论注数在预算与上限内）")

    return {
        "单注EV": round(float(ev_per_ticket), 6),
        "kelly比例": round(float(f), 6),
        "kelly缩水": frac,
        "理论注数": n_theory,
        "预算注数": n_budget,
        "实际建议注数": n_final,
        "触发约束": constraints,
        "资金状态": bp["状态"],
        "诚实边界": _HONEST_NOTE,
    }
