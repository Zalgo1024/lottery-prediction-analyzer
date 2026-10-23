# -*- coding: utf-8 -*-
"""ev/tier_allocate.py —— 预算 × 倍数分配（Phase C，诊断层，不接生产）。

这一层要回答的唯一问题
----------------------
「同一笔预算 B 元（= N 注，每注 2 元），把钱摊成 N 张不同票，还是压成更少票 × 倍数？」

三条必须先摆明的事实（否则整层会被误读）
------------------------------------------
1. **对「达标」目标，倍数是数学空操作。**
   达标 = 「至少一注达到 ≥目标奖级」。同一张票买 2 倍不会让任何新结局被覆盖，
   所以 `P(达标)` 只由**去重后的票集**决定，倍数完全无关。可证明，不是经验结论。
   ⇒ 只要倍数挤掉了不同的票，`P(达标)` **弱下降到严格下降**。
   ⇒ `all_single` 在达标口径上**弱占优**，本模块用实测把这个代价量化出来。

2. **对「金额」目标，倍投可能有作用。** 净额分布的尾部会被放大，
   `P(净额 ≥ 0)` 未必单调。这是**待检验假设**，不是结论 —— 故本模块同时测
   `盈利期占比`，允许它给出与第 1 条相反的答案。

3. **跨 K / 跨目标一律按每 100 元归一**（`达标期数/100元`、`净收益/100元`），
   否则 K 大的一侧靠「多花钱」赢，比较没有意义。

诚实边界
--------
本模块**不主张**任何分配方式能提高中奖机会；它只把「集中 vs 摊薄」的代价与
金额侧的变化**量出来**。结论若指向倍数，必须先排除「单期集中度」与「少数大奖」
驱动（见 `单次大奖占总实得比`）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

OBJECTIVES: tuple = ("all_single", "few_double", "incremental_coverage", "monetary_threshold")

# few_double 的「被加倍票占比」网格（占去重注数的比例）
N2_RATIOS: tuple = (0.05, 0.10, 0.20)

OBJECTIVE_NOTE = {
    "all_single": "N 张互异票各 1 倍（基线：达标口径下的弱占优者）",
    "few_double": "把最边际的 m 张撤掉，改为给最有效的 m 张各加 1 倍（去重数 N−m）",
    "incremental_coverage": "把最边际的 m 张加倍（对照：预期更差，用来看代价有多大）",
    "monetary_threshold": "倍数给「单注期望奖金 ≥ T」的票；κ=0 时全部票期望相同 ⇒ 退化",
}


# ---------------------------------------------------------------- 分配
def allocate_multipliers(tickets: Sequence[Dict[str, Any]], n_bets: int,
                         objective: str = "all_single",
                         n2_ratio: float = 0.10,
                         weights: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """把 `n_bets` 注预算分配到 `tickets`（已按「贪婪边际从大到小」排序）上。

    返回 `{"objective", "总注数", "去重注数", "倍数分布", "票": [(ticket, mult)], "说明"}`。
    ★ `tickets` 的顺序有语义：越靠前 = 边际覆盖贡献越大（`select_tier_coverage` 的选出序）。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"未知 objective {objective!r}，可选 {OBJECTIVES}")
    n_bets = int(n_bets)
    if n_bets <= 0:
        raise ValueError("预算必须 > 0")
    tickets = list(tickets)
    if not tickets:
        raise ValueError("候选票为空")

    note = OBJECTIVE_NOTE[objective]
    if objective == "all_single":
        use = tickets[:n_bets]
        plan = [(t, 1) for t in use]
        if len(plan) < n_bets:
            note += f"；池子不足，只摊出 {len(plan)}/{n_bets} 注"

    elif objective in ("few_double", "incremental_coverage"):
        m = max(1, int(round(n_bets * float(n2_ratio))))
        m = min(m, n_bets // 3)                     # 保证仍有 3/5 以上的去重注数
        distinct = n_bets - m
        if len(tickets) < distinct:
            raise ValueError(f"候选票 {len(tickets)} 张不足去重需求 {distinct} 张")
        use = tickets[:distinct]
        if objective == "few_double":
            hot = list(range(min(m, distinct)))     # 加给边际最大的
            note += f"；m={m}（{n2_ratio:.0%}）"
        else:
            hot = list(range(max(0, distinct - m), distinct))   # 加给边际最小的（对照）
            note += f"；m={m}（{n2_ratio:.0%}，刻意加给最边际票）"
        plan = [(t, 1) for t in use]
        for i in hot:
            plan[i] = (plan[i][0], plan[i][1] + 1)

    else:   # monetary_threshold
        if weights is None:
            # κ=0 时所有票的单注期望奖金完全相同 ⇒ 该目标没有独立含义，退化为 all_single
            use = tickets[:n_bets]
            plan = [(t, 1) for t in use]
            note += "；**未提供期望奖金权重 ⇒ κ=0 下所有票期望相同，本目标退化为 all_single，"
            note += "不具独立含义**"
        else:
            m = max(1, int(round(n_bets * float(n2_ratio))))
            m = min(m, n_bets // 3)
            distinct = n_bets - m
            order = sorted(range(len(tickets)), key=lambda i: -float(weights[i]))
            use = [tickets[i] for i in order[:distinct]]
            plan = [(t, 1) for t in use]
            for i in range(min(m, distinct)):
                plan[i] = (plan[i][0], plan[i][1] + 1)
            note += f"；m={m}（按期望奖金降序加倍）"

    total = sum(m for _, m in plan)
    dist: Dict[int, int] = {}
    for _, m in plan:
        dist[m] = dist.get(m, 0) + 1
    return {
        "objective": objective,
        "总注数": total,
        "去重注数": len(plan),
        "倍数分布": dict(sorted(dist.items())),
        "票": plan,
        "说明": note,
    }


# ---------------------------------------------------------------- 结算（带倍数）
def evaluate_period_weighted(lottery: str, plan: Sequence[Tuple[Dict[str, Any], int]],
                             actual_rec: Any, issue: Any, cache: Dict[str, Any],
                             target_grades: Optional[frozenset] = None) -> Dict[str, Any]:
    """带倍数的一期结算。

    ★ 达标只看**去重票集**（倍数与达标无关，见模块文档第 1 条）；实得按倍数放大。
    """
    from ev.coverage_backtest import grade_ticket

    staked = 0
    total = 0.0
    wins = 0
    hit = False
    ge5 = ge10 = False
    for t, m in plan:
        m = int(m)
        staked += m
        tier, payout, _note = grade_ticket(lottery, t, actual_rec, issue, cache)
        if payout and payout > 0:
            total += payout * m
            wins += m
            if payout >= 5:
                ge5 = True
            if payout >= 10:
                ge10 = True
        if target_grades is not None and tier and tier != "未中" and tier in target_grades:
            hit = True
    return {
        "投注注数": staked,
        "成本": staked * 2.0,
        "实得": round(total, 2),
        "中奖注数": wins,
        "≥5元": ge5,
        "≥10元": ge10,
        "达标": hit,
    }


# ---------------------------------------------------------------- 回放
# 最大回撤与达标回测共用一个实现（单一真源，避免两处口径漂移）
from ev.tier_backtest import _max_drawdown  # noqa: E402,F401


def run_alloc_arm(lottery: str, objective: str, K: int, target: Any = None,
                  periods: Optional[int] = None, seed: int = 0,
                  pool_size: int = 2000, cap: Optional[int] = None,
                  reselect_every: int = 25, n2_ratio: float = 0.10) -> Dict[str, Any]:
    """按某个分配目标回测。预算 = 2K 元（即 K 注）。

    ★ 所有 objective 共享**同一批**去重票（都来自 `select_tier_coverage` 的选出序），
      差别只在「摊薄还是加倍」——否则比的就不是分配了。
    """
    from ev import tier_target as TT
    from ev.coverage_backtest import build_pool
    from ev.lowcrowd_backtest import PROD_OVERLAP_CAP
    from ev.profit.rules import rules_for
    from ev.tier_backtest import load_test_recs
    from ev.tier_coverage import select_tier_coverage

    cap = int(PROD_OVERLAP_CAP if cap is None else cap)
    n_bets = int(K)                                  # 预算 2K 元 ⇒ K 注
    recs = load_test_recs(lottery, periods)
    if not recs:
        return {"期数": 0, "降级": "无开奖记录"}

    gcache: Dict[str, frozenset] = {}

    def grades_of(issue):
        ver = rules_for(lottery, issue).rule_version
        g = gcache.get(ver)
        if g is None:
            g = gcache[ver] = TT.resolve_conditions(lottery, target, issue)["grades"]
        return g

    cache: Dict[str, Any] = {}
    series_net: List[float] = []
    series_hit: List[int] = []
    series_pay: List[float] = []
    hit = ge5 = ge10 = profit = 0
    staked_total = 0
    total = 0.0
    wins = 0
    max_single_cost = 0
    alloc_meta: Dict[str, Any] = {}

    step = max(1, int(reselect_every))
    for blk, start in enumerate(range(0, len(recs), step)):
        chunk = recs[start:start + step]
        pool = build_pool(lottery, pool_size, seed=seed * 100003 + blk)
        # 去重票集：先按目标选出 **n_bets 张**（= all_single 的摊薄上限），
        # 保证任何 objective 都有素材；然后由分配函数决定撤掉/加倍哪些。
        tickets, meta = select_tier_coverage(lottery, pool, n_bets, target, chunk[0].期号,
                                            cap=cap, seed=seed * 1009 + blk)
        alloc = allocate_multipliers(tickets, n_bets, objective, n2_ratio)
        if not alloc_meta:
            alloc_meta = {k: v for k, v in alloc.items() if k != "票"}
            alloc_meta["池子实际注数"] = meta["选中注数"]
        for rec in chunk:
            ev = evaluate_period_weighted(lottery, alloc["票"], rec, rec.期号, cache,
                                          grades_of(rec.期号))
            series_pay.append(ev["实得"])
            series_net.append(round(ev["实得"] - ev["成本"], 2))
            series_hit.append(1 if ev["达标"] else 0)
            staked_total += ev["投注注数"]
            total += ev["实得"]
            wins += ev["中奖注数"]
            max_single_cost = max(max_single_cost, ev["成本"])
            hit += 1 if ev["达标"] else 0
            ge5 += 1 if ev["≥5元"] else 0
            ge10 += 1 if ev["≥10元"] else 0
            profit += 1 if ev["实得"] >= ev["成本"] else 0

    n = len(series_hit)
    cost = staked_total * 2.0
    net = total - cost
    per100 = (cost / 100.0) or 1.0
    mdd, mdd_len = _max_drawdown(series_net)
    top_share = (max(series_pay) / total) if total > 0 else 0.0
    res_latest = TT.resolve_conditions(lottery, target, recs[-1].期号)
    return {
        "彩种": lottery, "臂": objective, "目标": res_latest["label"],
        "期数": n, "预算注数": n_bets, "去重注数": alloc_meta.get("去重注数"),
        "倍数分布": alloc_meta.get("倍数分布"),
        "期均投入": round(cost / n, 2) if n else 0.0,
        "总投注注数": staked_total,
        "总实得": round(total, 2),
        "净收益": round(net, 2),
        "达标期数": hit,
        "达标期占比": round(hit / n, 4) if n else 0.0,
        "≥10元期占比": round(ge10 / n, 4) if n else 0.0,
        "≥5元期占比": round(ge5 / n, 4) if n else 0.0,
        "盈利期占比": round(profit / n, 4) if n else 0.0,
        "中奖次数": wins,
        # ---- 每 100 元归一（跨 K / 跨目标唯一可比口径）----
        "达标期数/100元": round(hit / per100, 6),
        "盈利期数/100元": round(profit / per100, 6),
        "净收益/100元": round(net / per100, 6),
        # ---- 风险/集中度（防止结论被少数大奖驱动）----
        "最大回撤": mdd,
        "最大回撤持续期数": mdd_len,
        "单期最大投入": round(max_single_cost, 2),
        "单次大奖占总实得比": round(top_share, 4),
        "中奖注数": wins,
        "序列_净额": series_net,
        "序列_实得": series_pay,
        "序列_达标": series_hit,
        "目标Kp": min(1.0, n_bets * TT.single_ticket_probability(
            lottery, res_latest["conditions"], recs[-1].期号)),
        "说明": alloc_meta.get("说明"),
        "口径提示": ("达标只看去重票集，倍数与之无关；倍数只放大实得与净额波动。"
                     "净收益类指标必须同时看『单次大奖占总实得比』——"
                     "少数一两期就能翻转结论。"),
    }


def compare_alloc(lottery: str, K: int, target: Any = None,
                  objectives: Sequence[str] = OBJECTIVES,
                  periods: Optional[int] = None, seeds: Sequence[int] = (0,),
                  pool_size: int = 2000, cap: Optional[int] = None,
                  reselect_every: int = 25,
                  n2_ratios: Sequence[float] = N2_RATIOS) -> Dict[str, Any]:
    """多目标对照。倍数目标的 n2_ratio 取网格平均（避免单点运气）。"""
    runs: Dict[str, List[Dict[str, Any]]] = {o: [] for o in objectives}
    for s in seeds:
        for o in objectives:
            # monetary_threshold 在无权重时退化为 all_single ⇒ 不必扫 ratio 网格
            ratios = (n2_ratios if o in ("few_double", "incremental_coverage") else (0.10,))
            for r in ratios:
                runs[o].append(run_alloc_arm(lottery, o, K, target=target,
                                             periods=periods, seed=s,
                                             pool_size=pool_size, cap=cap,
                                             reselect_every=reselect_every, n2_ratio=r))
    agg: Dict[str, Any] = {}
    for o, lst in runs.items():
        lst = [x for x in lst if x.get("期数")]
        if not lst:
            agg[o] = {"降级": "无有效回测"}
            continue
        n = len(lst)

        def mean(key):
            return round(sum(x[key] for x in lst) / n, 6)

        agg[o] = {
            "期数": lst[0]["期数"],
            # ★ 必须报**网格**而不是 lst[0] 的单点：few_double / incremental_coverage
            #   在全距上取 n2_ratio 网格平均，各点去重注数不同（m=round(K·r)）。
            #   只报第一个点会让人误以为「这条臂只用了 190 注」，
            #   而它的达标期占比其实是 190/180/160 三个点的平均。
            "去重注数": sorted({int(x["去重注数"]) for x in lst}, reverse=True),
            "达标期占比": mean("达标期占比"),
            "盈利期占比": mean("盈利期占比"),
            "达标期数/100元": mean("达标期数/100元"),
            "净收益/100元": mean("净收益/100元"),
            "最大回撤": mean("最大回撤"),
            "单次大奖占总实得比": mean("单次大奖占总实得比"),
            "达标期占比_全距": round(max(x["达标期占比"] for x in lst)
                                   - min(x["达标期占比"] for x in lst), 6),
            "盈利期占比_全距": round(max(x["盈利期占比"] for x in lst)
                                   - min(x["盈利期占比"] for x in lst), 6),
            "样本数": n,
        }
    base = "all_single"
    if base in agg and isinstance(agg[base], dict) and "达标期占比" in agg[base]:
        deltas = {}
        for o in objectives:
            if o == base or "达标期占比" not in agg.get(o, {}):
                continue
            d_hit = agg[o]["达标期占比"] - agg[base]["达标期占比"]
            d_prof = agg[o]["盈利期占比"] - agg[base]["盈利期占比"]
            noise = max(agg[o].get("达标期占比_全距", 0.0),
                        agg[base].get("达标期占比_全距", 0.0))
            noise_prof = max(agg[o].get("盈利期占比_全距", 0.0),
                             agg[base].get("盈利期占比_全距", 0.0))
            deltas[o] = {
                "达标差(对all_single)": round(d_hit, 6),
                "盈利差(对all_single)": round(d_prof, 6),
                "达标差是否超噪声": bool(abs(d_hit) > noise),
                "盈利差是否超噪声": bool(abs(d_prof) > noise_prof),
                "跨seed全距": noise,
                "盈利跨seed全距": noise_prof,
                "说明": ("「盈利期」在本口径下是**个位数事件**（300 期里 1~5 期），"
                         "全距通常远大于差本身 ⇒ 这条列只作报告，不作判定。"),
            }
    else:
        deltas = {}
    return {
        "彩种": lottery, "K": K, "预算注数": K, "目标": target,
        "seeds": list(seeds), "n2_ratios": list(n2_ratios),
        "臂": agg,
        "对基线的差": deltas,
        "结论纪律": ("① 达标口径上倍数只可能变差（把不同的票换成重复票，可证明）；"
                     "② 盈利口径若变好，必须同时看『单次大奖占总实得比』与跨 seed 全距，"
                     "不许用单点运气下结论；③ 全篇不主张「提高中奖机会」。"),
    }
