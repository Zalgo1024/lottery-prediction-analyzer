"""ev/ —— 结构性边建模（限号/撞号 EV + 决策层）

rulebook.py   赔率表 + 概率（名义 EV 地基）
crowd.py      公众投注分布估计（位级热度模型，限号/撞号暴露地基）
engine.py     net_ev：名义EV / 限号EV / 限号暴露概率 + scan_issues 冷门TOP
rollover.py   双/大 Rollover-EV（奖池滚动窗口判定，任务2.3）
sampler.py    约束采样出号（反大众+形态约束+低重叠，任务2.4）
payout.py     实得奖金统一口径（NetPayout WP0：概率×实得单注+净EV）
kelly.py      Kelly 下注比例 + 蒙特卡洛模拟（任务2.1）
bankroll.py   资金/风险预算（单期预算/止损/熔断，任务2.2）
"""

from .rulebook import get_rulebook, nominal_ev, COST
from .crowd import fit_crowd_model, CrowdModel
from .engine import net_ev, scan_issues
from .rollover import rollover_ev, rollover_history, fixed_prize_ev
from .sampler import sample_tickets
from .payout import payout_eff, expected_payout, net_ev_payout, share_multiplier, grade_probabilities
from .popularity import fit_popularity, counterfactual_backtest, get_popularity_model, ticket_mu
from .selection import select_tickets
from .kelly import kelly_fraction, kelly_from_ev, monte_carlo_sim
from .bankroll import Budget, budget_plan

__all__ = [
    "get_rulebook",
    "nominal_ev",
    "COST",
    "fit_crowd_model",
    "CrowdModel",
    "net_ev",
    "scan_issues",
    "rollover_ev",
    "rollover_history",
    "fixed_prize_ev",
    "sample_tickets",
    "payout_eff",
    "expected_payout",
    "net_ev_payout",
    "share_multiplier",
    "grade_probabilities",
    "fit_popularity",
    "counterfactual_backtest",
    "get_popularity_model",
    "ticket_mu",
    "select_tickets",
    "kelly_fraction",
    "kelly_from_ev",
    "monte_carlo_sim",
    "Budget",
    "budget_plan",
]
