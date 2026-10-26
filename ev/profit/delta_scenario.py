# -*- coding: utf-8 -*-
"""ev/profit/delta_scenario.py —— 「中奖后能分到多少」的**情景区间**（v1.8 用户指令二）。

问题
----
用户点明了项目的核心瓶颈：**没有分组合的实际购买量**。所以「冷门组合更值钱」
不是一个可以测出来的规律，只能是一个**假设变量** δ：

    δ = 若开出的正好是我持有的组合，则该组合的其他购买人数，相对基准的减少比例。

δ=0 ⇒ 我与平均热度相同；δ=1 ⇒ 除我之外无人买这个组合（上界情景）。

★ 报告形态必须是**区间**，不得取单点。任何 δ>0 的结论都是「若…则…」。

v1.7 的口径错误（本模块存在的第二个理由）
--------------------------------------
v1.7 用 κ × z_票 缩放**所有**浮动奖的中奖注数。这在一等奖上是对的（一个完整组合
= 一张票，买它的人越少，我分得越多），但在二等奖及以下就不成立：那几档的分摊
分母由**总注数**决定（双色球二等奖约 143 注/期），与我这一注组合的冷门度无关。
v1.7 那个「唯一正情景 +6.31 元/100元」全部来自把 δ 错误地施加到二等奖上。

★ 主口径（v1.8 登记）：δ **只作用于一等**。
★ 旧口径（δ 作用于全部浮动奖）同时报，但标「模型外推，不得作实投依据」，
  目的就是让上面那个 +6.31 能被追溯到它来自哪里。

★ 一处必须说清的保守简化：双色球二等奖的分母其实是「同一红球组合的购买注数」
  （二等 6+0 覆盖 15 个蓝球），严格说也会受红球组合冷门度影响。但项目没有分组合
  购买量数据 ⇒ 主口径把它固定在基准（δ 不作用）。这个方向的偏差是**低估**我这一注
  的实得，属于保守一侧，故与「取更保守者」的纪律一致。

结构性上限（与奖池规模无关）
--------------------------
δ 再大，一等奖也只能涨到「我独享且被单注封顶截断」：`P(一等) × 封顶税后`。
若这个上限仍补不上「转正所需缺口」⇒ **无论奖池多大、δ 多接近 1，都不可能转正**。
这条是本模块最重要的判决输出，它不需要任何情景假设。
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Dict, Iterable, Optional

from ev.profit.payouts import CAP_DEFAULT, Ctx, build_ctx, expected_payout
from ev.profit import tax as TX

# 只有「中奖注数 = 买我这注完整组合的人数」的奖级，δ 才直接作用其上
COMBINATION_SPECIFIC = ("一等",)

DELTA_GRID = (0.0, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0)
POOL_ERROR_GRID = (-0.30, -0.15, 0.0, 0.15, 0.30)

HONEST_NOTE = (
    "δ 是**假设情景变量**，不是已测出的市场规律：项目没有分组合的实际购买量，"
    "现有冷门分数只是代理。结论只在「若 δ 取该值」的前提下成立；"
    "报告形态是区间，引用时不得取单点。"
    "另：彩票资金按游戏规则分为奖金、发行费与公益金，只有奖金部分构成返奖来源，"
    "故长期返奖率显著低于 1——「经常中小奖」不等于回本。"
)


def after_tax(gross: float) -> float:
    """单注计税：超 1 万元部分按 20% 缴（口径见 ev/profit/tax）。"""
    return float(gross) - max(0.0, float(gross) - TX.TAX_THRESHOLD) * TX.TAX_RATE


def grade_probabilities(ctx: Ctx) -> Dict[str, float]:
    """每奖级的**单注**命中概率（外层 × 内层组合数精确）。"""
    r = ctx.rules
    n_out, k_out = int(r.outer_range[1]), int(r.outer_width)
    m_in = max(j for (_h, j) in r.grade_table)
    from ev.tier_coverage import _inner_pool_size
    n_in = _inner_pool_size(r, m_in)
    tot_out = math.comb(n_out, k_out)
    tot_in = math.comb(n_in, m_in)
    out: Dict[str, float] = {}
    for (h, j), g in r.grade_table.items():
        p = (math.comb(k_out, h) * math.comb(n_out - k_out, k_out - h) / tot_out) * \
            (math.comb(m_in, j) * math.comb(n_in - m_in, m_in - j) / tot_in)
        out[g] = out.get(g, 0.0) + p
    return out


def _ctx_with_pool(ctx: Ctx, pool_error: float) -> Ctx:
    """奖池误差 ±x 作用在**浮动池**上（不是作用在实得奖金上）。

    ★ 必须先缩池再算分摊：先算实得再乘 (1+ε) 会把单注封顶也乘掉，
      等于偷偷取消封顶（封顶是规则，不是误差项）。
    """
    if not pool_error:
        return ctx
    return replace(ctx, float_pool={k: float(v) * (1.0 + float(pool_error))
                                    for k, v in ctx.float_pool.items()})


def _mu_others(ctx: Ctx, grade: str, p: float, delta: float, *,
               legacy: bool) -> float:
    """「别人」在该奖级上的中奖注数。

    主口径：只有 `COMBINATION_SPECIFIC` 的奖级按 (1−δ) 缩；
    旧口径：全部浮动奖都缩（v1.7 的错误做法，仅供追溯）。
    """
    mu = float(ctx.total_tickets) * float(p)
    gr = ctx.rules.grades.get(grade)
    floating = gr is not None and gr.kind != "fixed"
    if floating and (grade in COMBINATION_SPECIFIC or legacy):
        mu *= (1.0 - max(0.0, min(1.0, float(delta))))
    return mu


def evaluate(ctx: Ctx, delta: float, pool_error: float = 0.0, *,
             legacy: bool = False) -> Dict[str, Any]:
    """单注税后净 EV（元），并给出**分奖级**明细（用于看清钱到底来自哪一档）。"""
    c = _ctx_with_pool(ctx, pool_error)
    ps = grade_probabilities(c)
    price = float(c.rules.avg_ticket_price)
    total_net = 0.0
    detail: Dict[str, Dict[str, Any]] = {}
    for g, p in ps.items():
        gr = c.rules.grades.get(g)
        if gr is None:
            continue
        mu = _mu_others(c, g, p, delta, legacy=legacy)
        pay = float(expected_payout(c, g, mu) or 0.0)
        net = float(p) * after_tax(pay)
        total_net += net
        detail[g] = {
            "kind": gr.kind,
            "p": float(p),
            "mu_others": mu,
            "期望实得": pay,
            "贡献税后_元每注": net,
            "封顶": (float(gr.cap) if getattr(gr, "cap", None) else None),
        }
    return {"δ": float(delta), "奖池误差": float(pool_error), "口径": (
        "旧(δ作用全部浮动奖)" if legacy else "主(δ只作用一等)"),
        "净EV_元每注": total_net - price,
        "净EV_元每百元": (total_net - price) / price * 100.0 if price else float("nan"),
        "实得合计_元每注": total_net, "票价": price, "分奖级": detail}


def structural_bound(ctx: Ctx) -> Dict[str, Any]:
    """与奖池规模无关的 δ 硬界，以及「它能否补上转正缺口」。

    `一等奖贡献上限 = P(一等) × 封顶税后` —— 这是 δ→1 且封顶绑定时的极值。
    """
    ps = grade_probabilities(ctx)
    p1 = float(ps.get("一等", 0.0))
    price = float(ctx.rules.avg_ticket_price)
    gr = ctx.rules.grades.get("一等")
    cap = float(gr.cap) if (gr is not None and getattr(gr, "cap", None)) else CAP_DEFAULT

    base = evaluate(ctx, 0.0)
    cur = float((base["分奖级"].get("一等") or {}).get("贡献税后_元每注", 0.0))
    cap_contribution = p1 * after_tax(cap)
    gain_max = cap_contribution - cur
    gap = price - float(base["实得合计_元每注"])      # 转正还差多少（元/注）

    return {
        "P_一等": p1, "单注封顶": cap, "封顶税后": after_tax(cap),
        "一等贡献_当前_元每注": cur,
        "一等贡献_上限_元每注": cap_contribution,
        "δ可争取总量_元每注": gain_max,
        "δ可争取占票价比": gain_max / price if price else float("nan"),
        "转正所需缺口_元每注": gap,
        "δ上限能否补上缺口": bool(gain_max >= gap),
        "_含义": ("一等奖贡献最多抬到 P(一等)×封顶税后。若这个上限减当前值仍小于"
                  "转正缺口 ⇒ 无论奖池多大、δ 多接近 1，都不可能转正。"
                  "该界不依赖任何情景假设。"),
        "基准": base,
    }


def delta_grid(ctx: Ctx, *, deltas: Iterable[float] = DELTA_GRID,
               pool_errors: Iterable[float] = POOL_ERROR_GRID,
               legacy: bool = False) -> Dict[str, Any]:
    """δ × 奖池误差的整段曲面（主口径与旧口径各跑一次）。"""
    rows = [evaluate(ctx, d, e, legacy=legacy)
            for d in deltas for e in pool_errors]
    positive = [r for r in rows if r["净EV_元每注"] > 0]
    best = max(rows, key=lambda r: r["净EV_元每注"])
    return {
        "口径": "旧(δ作用全部浮动奖)" if legacy else "主(δ只作用一等)",
        "δ档": [float(d) for d in deltas],
        "奖池误差": [float(e) for e in pool_errors],
        "情景数": len(rows),
        "正情景数": len(positive),
        "最好情景": {k: best[k] for k in ("δ", "奖池误差", "净EV_元每注", "净EV_元每百元")},
        "正情景": [{k: r[k] for k in ("δ", "奖池误差", "净EV_元每注")} for r in positive],
        "行": rows,
    }


def delta_star(ctx: Ctx, *, eps: float = 0.0, legacy: bool = False,
               tol: float = 1e-9, max_iter: int = 60) -> Optional[float]:
    """转正所需的最小 δ（在 [0,1] 上二分）。

    ★ 返回 None 的两层含义必须分开：可能是「δ 到 1 也不够」（结构性），
      也可能是「δ=0 已经为正」（无需 δ）。调用方必须自己检查端点。
    """
    lo, hi = 0.0, 1.0
    f = lambda d: evaluate(ctx, d, eps, legacy=legacy)["净EV_元每注"]  # noqa: E731
    if f(0.0) > 0:
        return 0.0
    if f(1.0) < 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        if f(mid) >= 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return float(hi)


def scenario_report(lottery: str, issue: Any) -> Dict[str, Any]:
    """一个彩种一期的完整情景报告（主口径 + 旧口径 + 结构性上限 + δ*）。"""
    ctx = build_ctx(lottery, issue)
    main = delta_grid(ctx)
    old = delta_grid(ctx, legacy=True)
    bound = structural_bound(ctx)
    return {
        "彩种": lottery, "期号": str(issue), "规则版本": ctx.rules.rule_version,
        "票价": float(ctx.rules.avg_ticket_price),
        "总注数": float(ctx.total_tickets),
        "浮动池": {k: float(v) for k, v in ctx.float_pool.items()},
        "主口径": main, "旧口径": old,
        "结构性上限": bound,
        "δ*_主口径": delta_star(ctx),
        "δ*_主口径_奖池-30%": delta_star(ctx, eps=-0.30),
        "δ*_旧口径": delta_star(ctx, legacy=True),
        "诚实边界": HONEST_NOTE,
    }


__all__ = ["COMBINATION_SPECIFIC", "DELTA_GRID", "POOL_ERROR_GRID", "HONEST_NOTE",
           "after_tax", "grade_probabilities", "evaluate", "structural_bound",
           "delta_grid", "delta_star", "scenario_report"]
