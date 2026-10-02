"""
ev/threshold.py —— Rollover 门槛计算器（策略升级 E）

把 ev/rollover.py 的「这期是否参与」升级成「**要什么条件才可能转正**」：

  参与判定（rollover 口径）：split_margin = (池×pool_share/cap) / μ ≥ 1
    ⇔ 解析门槛：池 ≥ cap × μ / pool_share

  精确门槛：解析式忽略泊松散布。实得 = E[min(cap, 池可分池/(1+X))]，
  X~Poisson(μ)——「E[逐点 min] ≥ θ·cap」比「逐点 min(cap, 池/μ) ≥ θ·cap」更严，
  故精确门槛（二分求根）恒 ≥ 解析门槛。

两档 μ：
  随机 μ_rand = λ = N×P(一等)（全市场平均分摊人数）
  冷门 μ_cold = λ×exp(β·f(q10 组合))（策略升级 A 的口径，缺省自动取）

三约束叠加成实践可行性判定：
  ① 头奖单注封顶（cap 本身）；
  ② 单期最大可购买注数；
  ③ 资金预算（ev.bankroll.Budget：单期预算 = 本金 2%）。

诚实边界（写死）：长期 EV≈−50%、25 条 edge 全 rejected 的结论不因门槛达成而改变；
门槛达成只说明「头奖单注估计顶格」，单期可买注数 × 边际贡献通常仍是分元级——
这是方差管理工具，不是收益工具。
"""
import logging
from typing import Any, Dict, Optional

from ev.payout import _SSQ_DLT_CAP, pool_share_expected

logger = logging.getLogger(__name__)

_HONEST_NOTE = (
    "门槛达成只说明「头奖单注估计顶格」；长期 EV≈−50%、25 条 edge 全 rejected "
    "的结论不因门槛达成而改变；单期可买注数×边际贡献通常为分元级——"
    "这是方差管理工具，不是收益工具。"
)

# 固定赔率彩种限号口径（ev/engine.py 同源）：限额 ≈ 当期销售额 × 49% / 单注奖金
_FIXED_LOTTERY = ("排列3", "福彩3D", "排列5", "七星彩")
_LIMIT_RATIO = 0.49


def pool_threshold(mu: float, cap: float = _SSQ_DLT_CAP,
                   pool_share: float = 0.7, theta: float = 0.999) -> Dict[str, Any]:
    """
    单档门槛。mu = 该档口径下「我中奖时的期望同注数」。

    返回 {"μ", "解析门槛_池", "精确门槛_池", "theta", "口径说明"}（门槛均指奖池金额）。
    """
    if mu <= 0:
        return {"μ": mu, "解析门槛_池": None, "精确门槛_池": None,
                "theta": theta,
                "口径说明": "μ 非正（无销售数据/模型回退且 λ 不可估），门槛不可估"}

    analytic = theta * cap * mu / pool_share

    # 精确门槛：对「池可分池」二分求根 f(hp) = E[min(cap, hp/(1+X))] − θ·cap
    hi = cap * (1.0 + mu + 10.0 * mu ** 0.5 + 50.0) / pool_share  # 必饱和的上界
    lo = 0.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if pool_share_expected(mid * pool_share, mu, cap) < theta * cap:
            lo = mid
        else:
            hi = mid
    precise = hi

    return {
        "μ": round(mu, 4),
        "解析门槛_池": round(analytic, 2),
        "精确门槛_池": round(precise, 2),
        "theta": theta,
        "口径说明": ("解析式=θ·cap·μ/pool_share（忽略泊松散布）；精确式=对 "
                     "E[min(cap,池可分池/(1+X))]=θ·cap 二分求根，恒 ≥ 解析式"),
    }


def feasibility_report(lottery: Optional[str] = None,
                       mu_cold: Optional[float] = None,
                       mu_rand: Optional[float] = None,
                       pool: Optional[float] = None,
                       bankroll: float = 1000.0,
                       max_tickets_per_period: Optional[int] = None,
                       sales: Optional[float] = None) -> Dict[str, Any]:
    """
    两档门槛 + 三约束 + 实践可行性判定。

    mu_rand 缺省 = λ（由 lottery 的最新期销量推，ev.rollover 口径）；
    mu_cold 缺省 = λ×冷门形态因子（ev.rollover._mu_cold_state，含显式降级）。
    """
    from ev.bankroll import Budget, budget_plan

    lam = None
    cold_basis = "μ_冷门（外部指定）" if mu_cold is not None else None
    if lottery is not None and (mu_rand is None or mu_cold is None):
        try:
            from ev.rollover import _P1, _mu_cold_state, _load_history
            df = _load_history(lottery)
            sales_row = df.iloc[0].get("总投注额")
            sales_val = float(sales_row) if sales_row == sales_row else 0.0
            n_tickets = sales_val / 2.0
            lam = n_tickets * _P1[lottery]
            if mu_rand is None:
                mu_rand = lam
            if mu_cold is None:
                factor, cold_basis = _mu_cold_state(lottery)
                mu_cold = lam * factor if (factor and lam > 0) else None
        except Exception as e:
            logger.debug(f"[threshold] 缺省 μ 推导失败 {lottery}: {e}")
            if mu_rand is None:
                mu_rand = None

    th_cold = pool_threshold(mu_cold) if (mu_cold and mu_cold > 0) else {
        "μ": mu_cold, "解析门槛_池": None, "精确门槛_池": None,
        "theta": 0.999, "口径说明": "μ_冷门不可估（流行度模型回退），门槛不可估"}
    th_rand = pool_threshold(mu_rand) if (mu_rand and mu_rand > 0) else {
        "μ": mu_rand, "解析门槛_池": None, "精确门槛_池": None,
        "theta": 0.999, "口径说明": "λ 不可估（无销售数据），门槛不可估"}

    bp = budget_plan(Budget(bankroll=bankroll))
    n_budget = int(bp["单期最大注数"])
    n_cap = max_tickets_per_period if max_tickets_per_period else None
    n_max = min(n_budget, n_cap) if n_cap else n_budget

    verdict = None
    margin_info: Dict[str, Any] = {}
    need_pool = th_cold.get("精确门槛_池")
    if need_pool is None:
        verdict = "门槛不可估（μ 缺失，诚实不参与）"
    elif pool is None or pool < need_pool:
        verdict = "池未达门槛"
    else:
        # 达门槛 → 算单期可买注数 × 冷-随机边际贡献
        try:
            from ev.rollover import _P1 as _p1_map
            p1 = _p1_map.get(lottery) if lottery else None
        except Exception:
            p1 = None
        if p1 is None:
            verdict = "达门槛但缺 P(一等奖) 无法核算边际"
        else:
            head_pool = pool * 0.7
            eff_cold = pool_share_expected(head_pool, mu_cold)
            eff_rand = pool_share_expected(head_pool, mu_rand)
            per_ticket = p1 * (eff_cold - eff_rand)
            total_margin = n_max * per_ticket
            margin_info = {
                "单期可买注数": n_max,
                "单注边际EV(冷门−随机)": round(per_ticket, 8),
                "单期边际合计": round(total_margin, 6),
            }
            if bp.get("状态") != "正常":
                verdict = "资金约束先于门槛触发（止损/熔断状态）"
            elif total_margin < 0.01:
                verdict = "达门槛但注数×边际贡献为分元级（方差管理工具，非收益工具）"
            else:
                verdict = ("达门槛且边际可观——但总 EV 仍为负，"
                           "边际只来自『中奖后少被分摊』，不改变中奖概率")

    out: Dict[str, Any] = {
        "彩种": lottery,
        "当前池": round(pool, 2) if pool is not None else None,
        "两档门槛": {
            "冷门(μ_cold)": th_cold,
            "随机(μ_rand=λ)": th_rand,
        },
        "三约束": {
            "①头奖单注封顶": _SSQ_DLT_CAP,
            "②单期最大可购买注数": n_max,
            "③资金预算": {"本金": bp["本金"], "单期预算(2%)": bp["单期预算(2%)"],
                         "状态": bp["状态"]},
        },
        **({"边际核算": margin_info} if margin_info else {}),
        "实践可行性判定": verdict,
        "诚实边界": _HONEST_NOTE,
    }
    if lam is not None:
        out["λ(当期推算)"] = round(lam, 4)
    if cold_basis:
        out["冷门口径来源"] = cold_basis
    if lottery in _FIXED_LOTTERY and sales:
        out["限号参考(固定赔率口径)"] = (
            f"限号限额 ≈ 销售额×{_LIMIT_RATIO:.0%}/单注奖金"
            f"（本彩种固定赔率，参与门槛计算不适用，仅展示可购性口径）")
    return out
