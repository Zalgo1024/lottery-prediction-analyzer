"""
ev/kelly.py —— Kelly 下注比例 + 蒙特卡洛模拟（任务 2.1）

核心纪律（数学结论，非建议）：
- Kelly f* = (b·p − q) / b（b=净赔率，q=1−p）。f* ≤ 0 ⇔ 单注 EV ≤ 0 → 不下注。
- 本项目所有彩种单注名义 EV 均为负（直选 −0.96 等），故 Kelly 的正确输出通常是「0（不下注）」。
- 只有当输入单注 EV > 0（如 Rollover 高奖池窗口的头奖修正 EV 或限号撞号修正后的七星彩冷门号）
  时 Kelly 才 > 0 —— 本模块用于「决定买不买、买多少」，而不是鼓励下注。
- 实际投注建议用「半 Kelly / 1/4 Kelly」+ 资金预算上限（见 ev/bankroll.py）。

蒙特卡洛：给定 单注EV/中奖概率/本金/仓位，模拟 n 期盈亏路径，输出
  终值分布（P5/P50/P95）、最大回撤、破产概率。用于检验「赢在算法亏在仓位」。
"""
import numpy as np


def kelly_fraction(p: float, net_odds: float) -> float:
    """
    Kelly 比例。p=单注中奖概率，net_odds=净赔率（如直选：payout/cost − 1 = 1040/2−1 = 519）。
    返回 f*（占可投注资金比例）。f* ≤ 0 → 不下注。
    """
    q = 1.0 - p
    if net_odds <= 0 or p <= 0 or p >= 1:
        return 0.0
    f = (net_odds * p - q) / net_odds
    return max(0.0, f)


def kelly_from_ev(ev_per_ticket: float, cost: float, prob: float,
                  frac: float = 0.5) -> float:
    """
    从单注 EV 反推 Kelly（含 EV≤0 时为 0）。
    frac: 缩水系数（半 Kelly=0.5、1/4 Kelly=0.25），默认 0.5（保守）。
    ev_per_ticket: 单注期望盈亏（元，正数才下注）。
    """
    if ev_per_ticket <= 0:
        return 0.0
    net_odds = max((ev_per_ticket + cost) / cost - 1.0, 1e-9)
    f = kelly_fraction(prob, net_odds)
    return max(0.0, f * frac)


def monte_carlo_sim(ev_per_ticket: float, prob: float, cost: float,
                    bankroll: float, stake_frac: float = 0.02,
                    n_periods: int = 100, n_paths: int = 10000,
                    seed: int = 7) -> dict:
    """
    蒙特卡洛模拟：每期投入 stake_frac×当前本金 买票，按概率中奖（+payout−cost）或未中（−cost）。
    返回：终值分布(P5/P50/P95)、平均最大回撤、破产率（<20% 本金）。
    """
    rng = np.random.default_rng(seed)
    payout = ev_per_ticket + cost  # 中奖净回报（含本金）
    p_win = prob
    # 每期中奖赔付倍数（相对当期投入）：中= payout/cost（投入的每注对应赔付）
    # 简化：每期投入 = stake_frac×bankroll，中奖返还 = 投入 × (payout/cost)
    mult_win = payout / cost if cost else 1.0

    finals = np.empty(n_paths)
    max_dd = np.empty(n_paths)
    broke = 0
    for i in range(n_paths):
        bal = bankroll
        peak = bankroll
        mdd = 0.0
        for _ in range(n_periods):
            stake = stake_frac * bal
            if stake < cost:
                break
            n_tickets = max(1, int(stake / cost))
            stake = n_tickets * cost
            if rng.random() < p_win:
                bal += stake * (mult_win - 1.0)
            else:
                bal -= stake
            peak = max(peak, bal)
            mdd = max(mdd, (peak - bal) / peak if peak > 0 else 0.0)
            if bal < bankroll * 0.2:
                broke += 1
                break
        finals[i] = bal
        max_dd[i] = mdd

    return {
        "初始本金": round(bankroll, 2),
        "每期仓位": f"{stake_frac*100:.0f}%",
        "期数": n_periods,
        "终值P5": round(float(np.percentile(finals, 5)), 2),
        "终值P50": round(float(np.percentile(finals, 50)), 2),
        "终值P95": round(float(np.percentile(finals, 95)), 2),
        "平均最大回撤": round(float(np.mean(max_dd)), 4),
        "破产率(跌破20%本金)": round(float(broke / n_paths), 4),
        "结论": _conclude(ev_per_ticket, np.percentile(finals, 50), bankroll),
    }


def _conclude(ev, p50, bankroll):
    if ev <= 0:
        return "单注EV≤0：Kelly=0，纪律要求不下注（本模拟仅为仓位纪律演示）"
    if p50 < bankroll:
        return "正EV但模拟中位数亏损：仓位过高或方差过大，建议降仓/半Kelly"
    return "正EV且模拟中位数盈利：可按 Kelly 缩水比例参与（仍属高风险娱乐）"


if __name__ == "__main__":
    import json
    # 示例：双色球高奖池窗口（头奖EV修正后）vs 排列3 直选（负EV）
    for label, ev, p in [
        ("双色球高奖池窗口(头奖贡献0.56)", -0.95, 1 / 17721088),
        ("排列3直选(名义EV)", -0.96, 1 / 1000),
    ]:
        f = kelly_from_ev(ev, 2.0, p)
        print(f"[{label}] Kelly缩水后={f:.6f}")
        if ev > 0:
            sim = monte_carlo_sim(ev, p, 2.0, 1000, stake_frac=f, n_periods=50)
            print(json.dumps(sim, ensure_ascii=False, indent=1))
