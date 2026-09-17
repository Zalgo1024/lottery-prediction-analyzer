"""
ev/bankroll.py —— 资金/风险预算（任务 2.2）

防「赢在算法亏在仓位」：无论单注 EV 如何，先定资金纪律——
- 单期预算上限（默认可投注资金的 2%）
- 止损线（累计亏损达本金 X% 停手）
- 连亏熔断（连续 N 期亏损强制休息）
- 单期可投注注数 = min(预算, 单期上限) / 单注成本
"""
from dataclasses import dataclass


@dataclass
class Budget:
    bankroll: float = 1000.0
    per_period_pct: float = 0.02          # 单期预算 = 本金 × 2%
    stop_loss_pct: float = 0.30           # 累计亏损达本金 30% 停手
    max_consec_loss: int = 5              # 连亏 5 期熔断
    cost_per_ticket: float = 2.0


def budget_plan(budget: Budget, consec_loss: int = 0,
                cumulative_pnl: float = 0.0) -> dict:
    """
    资金预算方案。
    consec_loss: 当前连亏期数
    cumulative_pnl: 累计盈亏（负=亏损）
    返回：单期预算/最大注数/熔断状态/止损状态/建议。
    """
    period_budget = budget.bankroll * budget.per_period_pct
    max_tickets = int(period_budget // budget.cost_per_ticket)
    max_tickets = max(1, max_tickets)

    stopped = False
    reasons = []
    if cumulative_pnl <= -budget.bankroll * budget.stop_loss_pct:
        stopped = True
        reasons.append(f"累计亏损 {cumulative_pnl:.0f} 达本金 {budget.stop_loss_pct*100:.0f}% → 止损停手")
    if consec_loss >= budget.max_consec_loss:
        stopped = True
        reasons.append(f"连亏 {consec_loss} 期 ≥ {budget.max_consec_loss} → 熔断休息")

    # 熔断/止损后恢复规则：回到本金 20% 以上且连亏清零才恢复
    if not stopped:
        advice = "正常可投"
    else:
        advice = "停止投注"

    return {
        "本金": round(budget.bankroll, 2),
        "单期预算(2%)": round(period_budget, 2),
        "单期最大注数": max_tickets,
        "止损线": f"-{budget.stop_loss_pct*100:.0f}%（{budget.bankroll*budget.stop_loss_pct:,.0f} 元）",
        "熔断阈值": f"连亏 {budget.max_consec_loss} 期",
        "当前连亏": consec_loss,
        "累计盈亏": round(cumulative_pnl, 2),
        "状态": "熔断/止损" if stopped else "正常",
        "建议": advice + ("；" + "；".join(reasons) if reasons else ""),
    }


if __name__ == "__main__":
    import json
    for consec, pnl in [(0, 0.0), (3, -120.0), (5, -100.0), (2, -400.0)]:
        r = budget_plan(Budget(bankroll=1000), consec_loss=consec, cumulative_pnl=pnl)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        print()
