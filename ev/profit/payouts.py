# -*- coding: utf-8 -*-
"""ev/profit/payouts.py —— 决策时点的奖金基准与浮动奖分摊。

口径（与 `ev/payout.py` 的分工）
-------------------------------
`ev/payout.payout_eff` 假设「号码不影响中奖概率」，只做奖金侧；
本模块是盈利模型的内核：**概率侧由 tilt.py 提供，奖金侧由本模块提供**。
两者只在「κ=0 且用当期实际奖金」时才应当一致（测试里交叉校验这一点）。

浮动奖的池怎么来（★ 不用「奖池×0.7」那种拍脑袋系数）
-----------------------------------------------------
一等的可分配池 = **进入本期的奖池 + 本期新增的一等份额**：
    pool_g1 = pool_before + a1 × Ŝ
其中 a1 由历史实测中位数标定（双色球：二等总额/销售额 中位 0.0684、n=2760，
一二等合计 0.2251 ⇒ a1 ≈ 0.157）。封顶 1000 万会自动压住池子大小的影响——
池子再厚，单注也只到 cap，所以 a1 的误差对净EV 的影响远小于机制强度 κ。

★ 诚实标注每个常数是「实测」还是「先验」；`口径` 字段必须随数字一起输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ev.payout import pool_share_expected
from ev.profit import pit
from ev.profit.rules import LotteryRules, rules_for

# 一等奖份额 a1（占销售额）与二等份额（占销售额）
#   双色球：二等实测中位 0.0684（n=2760）；一二等合计实测 0.2251 ⇒ 一等 ≈ 0.157
#   大乐透：二等实测中位 0.0414（n=1241）；一等按同类结构取先验（未校准）
FLOAT_SHARE = {
    "双色球": {"一等": (0.157, "实测中位标定 n=2760"), "二等": (0.0684, "实测中位 n=2760")},
    "大乐透": {"一等": (0.115, "先验（未校准）"), "二等": (0.0414, "实测中位 n=1241")},
    "七星彩": {"一等": (0.06, "先验（未校准）"), "二等": (0.03, "先验（未校准）")},
}

FLOAT_SOURCE = {
    "双色球": "rule_empirical",
    "大乐透": "prior_partial",
    "七星彩": "prior_only",
    "排列3": "fixed_odds",
    "福彩3D": "fixed_odds",
    "排列5": "fixed_odds_risk_control",
}

CAP_DEFAULT = 10_000_000.0


@dataclass
class Ctx:
    """决策时点的全部已知量（全部来自 PitView，天然不含未来）。"""
    lottery: str
    rules: LotteryRules
    pool_before: Optional[float]
    sales_forecast: float
    total_tickets: float
    float_pool: Dict[str, float] = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def payout_basis(self, grade: str) -> Optional[float]:
        """该奖级的「单注基准奖金」。

        固定奖 → 直接是金额；浮动奖 → 可分配池（真正到手还要除以 1+X 并封顶）。
        """
        r = self.rules.grades.get(grade)
        if r is None:
            return None
        if r.kind == "fixed":
            return float(r.fixed)
        return self.float_pool.get(grade)


def build_ctx(lottery: str, issue: Any, sales_override: Optional[float] = None) -> Ctx:
    """从 PIT 视图构造决策上下文。"""
    r = rules_for(lottery)
    view = pit.as_of(lottery, issue)
    warn = list(view.warnings)

    fc = view.forecast_sales()
    sales = float(sales_override if sales_override is not None else (fc["median"] or 0.0))
    if sales <= 0:
        warn.append("本期销量不可得（历史销售缺失）⇒ 浮动奖池与票量算不出来")

    pool_before = view.pool_before()
    shares = FLOAT_SHARE.get(lottery, {})
    float_pool: Dict[str, float] = {}
    for g, (a, src) in shares.items():
        base = a * sales
        if g == "一等":
            if pool_before is not None:
                base += pool_before
            elif lottery in ("双色球", "大乐透"):
                warn.append("缺少上期奖池 ⇒ 一等池只含本期新增部分（低估）")
        float_pool[g] = float(base)

    total = sales / float(r.avg_ticket_price) if sales > 0 else 0.0
    return Ctx(lottery=lottery, rules=r, pool_before=pool_before,
               sales_forecast=sales, total_tickets=total,
               float_pool=float_pool, warnings=warn)


def expected_payout(ctx: Ctx, grade: str, mu_others: float) -> float:
    """单注中该奖级时的**期望实得**。

    浮动奖：E[min(cap, 池/(1+X))]，X ~ Poisson(mu_others)，mu 是**别人**的注数
            （分母的 1 就是「我」）。
    ★ 禁止用 池/(1+mu)：1/(1+X) 是凸函数，朴素式系统性低估大奖实得。
    """
    r = ctx.rules.grades.get(grade)
    if r is None:
        return 0.0
    pool = ctx.payout_basis(grade)
    if pool is None:
        return 0.0
    if r.kind == "fixed":
        return float(pool)
    cap = float(r.cap) if r.cap else CAP_DEFAULT
    return pool_share_expected(float(pool), float(max(mu_others, 0.0)), cap)


def note(ctx: Ctx) -> Dict[str, Any]:
    """口径声明（必须随每份输出一起返回）。"""
    return {
        "奖金口径": FLOAT_SOURCE.get(ctx.lottery, "unknown"),
        "浮动奖份额": {g: {"a": a, "来源": s}
                       for g, (a, s) in FLOAT_SHARE.get(ctx.lottery, {}).items()},
        "票量": f"销售额 / {ctx.rules.avg_ticket_price:.4f}",
        "注意": ("浮动奖份额为实测中位/先验常数，未做删失回归；1000 万封顶会压住池子大小的影响，"
                 "故该常数的误差对净EV 的影响远小于机制强度 κ 的不确定性。"),
    }
