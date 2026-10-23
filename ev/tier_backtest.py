# -*- coding: utf-8 -*-
"""ev/tier_backtest.py —— 奖级达标策略：同预算对照回测（研究工具，不接生产）。

三臂（全部**同注数、同预算、同重叠上限、同候选池规模**）
--------------------------------------------------------
  random            同池随机（`ev.selection.control_tickets`）
  existing_coverage 现有覆盖选号（`ev.selection.select_coverage`，目标是 ≥5 元/六等，
                    **不是**本项目标）—— 用来回答「换目标到底有没有用」
  tier_coverage     本策略（`ev.tier_coverage.select_tier_coverage`，目标可参数化）

关于「walk-forward」
-------------------
三臂的选号都**不读历史开奖**（只读候选池 + 规则表），因此不存在前视；
这里没有需要训练的参数。所谓 walk-forward 退化为：
**每隔 `reselect_every` 期重抽候选池并重选一次**，
目的是把「选号这一次的随机性」也平均掉 —— 若整段历史只用同一批票，
三臂的差异只是**一次**抽样实现，方差大到无法判定。
★ 三臂在同一块用**同一个 seed**，保证逐期配对。

诚实判定三层（沿用低拥挤回测口径，不另立标准）
----------------------------------------------
1. 哨兵 = 单期平均中奖注数。各组合等可能 ⇒ 换选号**不改**这个量；
   它若动了，先查实现缺陷/前视泄漏，而不是宣布策略有效。
2. 判据 = 达标期占比（本策略目标），逐期配对 + 块 bootstrap（块长 `BETA_STEP`）。
3. 报告项 = ≥5 元 / ≥10 元 期占比、净收益 —— 只报不判。

★ 术语铁律：不显著只能写成「**功效不足以识别理论上限**」，
  **不许**写成「已证明无效 / 覆盖无效」。非显著 ≠ 无效果。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from ev import tier_target as TT
from ev.lowcrowd_backtest import (BETA_STEP, PROD_OVERLAP_CAP, block_bootstrap_ci,
                                  paired_report, sign_test)

logger = logging.getLogger(__name__)

ARMS: tuple = ("random", "existing_coverage", "tier_coverage")

DEFAULT_POOL = 2000
DEFAULT_RESELECT = 25
DEFAULT_PERIODS = 500

TERMINOLOGY_NOTE = (
    "不显著只能写成「功效不足以识别理论上限」，不得写成「已证明无效/覆盖无效」；"
    "非显著不等于无效果。"
)


# ---------------------------------------------------------------- 数据
def load_test_recs(lottery: str, periods: Optional[int] = None) -> List[Any]:
    """取用于回测的开奖记录（**升序**）。`periods=None` 用全量最近的 periods 期。"""
    from data.loader import load_lottery

    data = load_lottery(lottery)
    recs = sorted((r for r in (data.records or []) if getattr(r, "期号", None)),
                  key=lambda r: r.期号)      # ★ loader 返回降序，必须重排
    if periods:
        recs = recs[-int(periods):]
    return recs


def _grades_for(issue: Any, lottery: str, target: Any,
                cache: Dict[str, frozenset]) -> frozenset:
    """按期号取该期的**达标奖级集**（大乐透版本会变，必须逐期算）。"""
    from ev.profit.rules import rules_for

    ver = rules_for(lottery, issue).rule_version
    g = cache.get(ver)
    if g is None:
        g = cache[ver] = TT.resolve_conditions(lottery, target, issue)["grades"]
    return g


# ---------------------------------------------------------------- 选号
def _select(arm: str, lottery: str, pool: Sequence[Dict[str, Any]], K: int,
            target: Any, issue: Any, seed: int, cap: int):
    """按臂选号。返回 (票, meta)。三臂都必须给出**恰好 K 注**（否则成本口径被污染）。"""
    if arm == "random":
        from ev.selection import control_tickets

        return control_tickets(lottery, pool, K, seed=seed, cap=cap), {
            "策略": "同池随机", "重叠上限": cap}

    if arm == "existing_coverage":
        from ev.selection import select_coverage

        return select_coverage(lottery, pool, K, 5, seed, cap=cap), {
            "策略": "现有覆盖选号（目标 ≥5 元/六等，不是本项目标）", "重叠上限": cap}

    if arm == "tier_coverage":
        from ev.tier_coverage import select_tier_coverage

        return select_tier_coverage(lottery, pool, K, target, issue,
                                    cap=cap, seed=seed)

    raise ValueError(f"未知臂 {arm!r}，可选 {ARMS}")


def _max_drawdown(series: Sequence[float]) -> tuple:
    """净值曲线的最大回撤（金额）与最长持续期数。"""
    peak = cum = 0.0
    mdd = 0.0
    cur = longest = 0
    for x in series:
        cum += x
        if cum > peak:
            peak, cur = cum, 0
        else:
            cur += 1
            longest = max(longest, cur)
        mdd = min(mdd, cum - peak)
    return round(-mdd, 2), int(longest)


# ---------------------------------------------------------------- 回放
def _replay(lottery: str, plan: List[tuple], target_grades_of) -> Dict[str, Any]:
    """plan = [(期号, 开奖记录, 票列表), ...]（升序）。"""
    from ev.coverage_backtest import evaluate_period

    n = len(plan)
    if n == 0:
        return {"期数": 0}
    cache: Dict[str, Any] = {}
    wins = 0
    total = 0.0
    staked = 0
    ge5 = ge10 = hit = 0
    s_ge10: List[int] = []
    s_win: List[int] = []
    s_pay: List[float] = []
    s_hit: List[int] = []
    s_net: List[float] = []
    K = len(plan[0][2])

    for issue, rec, tks in plan:
        ev = evaluate_period(lottery, tks, rec, issue, cache,
                             target_grades=target_grades_of(issue))
        wins += int(ev["命中注数"])
        total += float(ev["总实得"])
        staked += len(tks)
        ok10 = 1 if ev["≥10元"] else 0
        ge5 += 1 if ev["≥5元"] else 0
        ge10 += ok10
        ok = 1 if ev.get("达标") else 0
        hit += ok
        s_ge10.append(ok10)
        s_win.append(int(ev["命中注数"]))
        s_pay.append(float(ev["总实得"]))
        s_hit.append(ok)
        s_net.append(round(float(ev["总实得"]) - len(tks) * 2.0, 2))

    # ★ 成本按**实际投注注数**累加（某期少出几注不许让它凭空变好）
    cost = staked * 2.0
    net = total - cost
    per100 = staked * 2.0 / 100.0 or 1.0
    mdd, mdd_len = _max_drawdown(s_net)
    top_pay = max(s_pay) if s_pay else 0.0
    top_share = (top_pay / total) if total > 0 else 0.0
    # ★ 剔除单期最大实得后的净收益。为什么必须单独给一列：
    #   双色球 K=200/500 期下，「三等」期望命中只有 0.91 次，实测某臂 2 次、另一臂 0 次
    #   ⇒ 单靠 2 次 3000 元就能把 `净收益/100元` 拉开 2.5 pp 以上，进而翻转臂间排序。
    #   剔除最大一期后三臂会一起落回「固定奖口径」（≈ −76），这才是可比的量。
    net_wo_top = net - top_pay
    return {
        "期数": n, "K": K, "总投注注数": staked,
        "≥5元期占比": round(ge5 / n, 4),
        "≥10元期占比": round(ge10 / n, 4),
        "中奖次数": wins,
        "单期平均中奖注数": round(wins / n, 4),
        "总实得": round(total, 2),
        "净收益": round(net, 2),
        "单期平均净收益": round(net / n, 4),
        "序列": {"≥10元": s_ge10, "中奖注数": s_win, "实得": s_pay},
        # ---- 本策略追加（不污染上面 11 个同构键）----
        "达标期数": hit,
        "达标期占比": round(hit / n, 4),
        "达标期数/100元": round(hit / per100, 6),
        "净收益/100元": round(net / per100, 6),
        "净收益/100元_剔除最大一期": round(net_wo_top / per100, 6),
        "序列_达标": s_hit,
        "成本": round(cost, 2),
        # 净收益的集中度：只报不判。少数一两期就能翻转净收益排序（见文档 §7.5）。
        "最大回撤": mdd,
        "最大回撤持续期数": mdd_len,
        "单期最大实得": round(top_pay, 2),
        "单次大奖占总实得比": round(top_share, 4),
    }


def run_tier_arm(lottery: str, arm: str, K: int, target: Any = None,
                 periods: Optional[int] = None, seed: int = 0,
                 pool_size: int = DEFAULT_POOL, cap: Optional[int] = None,
                 reselect_every: int = DEFAULT_RESELECT) -> Dict[str, Any]:
    """跑一条臂。返回指标 dict（含逐期序列，供配对比较）。"""
    from ev.coverage_backtest import build_pool

    if arm not in ARMS:
        raise ValueError(f"未知臂 {arm!r}，可选 {ARMS}")
    cap = int(PROD_OVERLAP_CAP if cap is None else cap)
    reselect_every = max(1, int(reselect_every))

    recs = load_test_recs(lottery, periods)
    if not recs:
        return {"期数": 0, "降级": "无开奖记录"}

    gcache: Dict[str, frozenset] = {}
    # 目标标签取自**最新一期**（仅供展示；逐期判定用的是 _grades_for）
    res_latest = TT.resolve_conditions(lottery, target, recs[-1].期号)

    plan: List[tuple] = []
    metas: List[Dict[str, Any]] = []
    for blk, start in enumerate(range(0, len(recs), reselect_every)):
        chunk = recs[start:start + reselect_every]
        pool = build_pool(lottery, pool_size, seed=seed * 100003 + blk)
        tks, meta = _select(arm, lottery, pool, K, target, chunk[0].期号,
                            seed=seed * 1009 + blk, cap=cap)
        metas.append(meta)
        for rec in chunk:
            plan.append((rec.期号, rec, tks))

    out = _replay(lottery, plan, lambda iss: _grades_for(iss, lottery, target, gcache))
    if out.get("期数", 0) == 0:
        return out

    meta0 = metas[0] if metas else {}
    from ev.mechanism.narrative import TIER_COVERAGE_CAVEAT

    out.update({
        "彩种": lottery,
        "臂": arm,
        "目标": res_latest["label"],
        "达标奖级": sorted(res_latest["grades"]),
        "规则版本": res_latest["rule_version"],
        "重叠上限": cap,
        "重选间隔": reselect_every,
        "重选次数": len(metas),
        "候选池规模": pool_size,
        "单注达标概率": TT.single_ticket_probability(
            lottery, res_latest["conditions"], recs[-1].期号),
        "理论上限Kp": min(1.0, K * TT.single_ticket_probability(
            lottery, res_latest["conditions"], recs[-1].期号)),
        "臂说明": meta0.get("策略", arm),
        "术语铁律": TERMINOLOGY_NOTE,
        "诚实边界": TIER_COVERAGE_CAVEAT,
    })
    if "通道权重和" in meta0:
        out["通道权重和(首块)"] = meta0["通道权重和"]
        out["并集概率上界(首块)"] = meta0.get("并集概率上界")

    # 饱和守卫：达标率恒 1 或 ≥5 元恒 1 ⇒ 该指标无区分力
    out["饱和"] = bool(out["达标期占比"] >= 0.999 or out["≥5元期占比"] >= 0.999)
    if out["饱和"]:
        out["饱和说明"] = ("达标率或 ≥5 元率已饱和（≈1）⇒ 该指标对选号无区分力，"
                           "不得据此判定策略优劣。")
    return out


# ---------------------------------------------------------------- 对照
def compare_tier(lottery: str, K: int, target: Any = None,
                 arms: Sequence[str] = ARMS, periods: Optional[int] = None,
                 seed: int = 0, seeds: Optional[Sequence[int]] = None,
                 pool_size: int = DEFAULT_POOL, cap: Optional[int] = None,
                 reselect_every: int = DEFAULT_RESELECT,
                 block: int = BETA_STEP, reps: int = 2000) -> Dict[str, Any]:
    """多臂 × 多 seed 对照。判据 = 达标期占比；哨兵 = 单期平均中奖注数。"""
    seeds = tuple(seeds) if seeds else (seed,)
    runs: Dict[str, List[Dict[str, Any]]] = {a: [] for a in arms}
    for s in seeds:
        for a in arms:
            runs[a].append(run_tier_arm(lottery, a, K, target=target,
                                        periods=periods, seed=s,
                                        pool_size=pool_size, cap=cap,
                                        reselect_every=reselect_every))
    if any(not r or r.get("期数", 0) == 0 for lst in runs.values() for r in lst):
        return {"降级": "某臂无有效回测", "彩种": lottery}

    agg: Dict[str, Any] = {}
    for a, lst in runs.items():
        n = len(lst)
        agg[a] = {
            "期数": lst[0]["期数"], "K": lst[0]["K"],
            "达标期占比": round(sum(r["达标期占比"] for r in lst) / n, 4),
            "≥10元期占比": round(sum(r["≥10元期占比"] for r in lst) / n, 4),
            "≥5元期占比": round(sum(r["≥5元期占比"] for r in lst) / n, 4),
            "单期平均中奖注数": round(sum(r["单期平均中奖注数"] for r in lst) / n, 4),
            "单期平均净收益": round(sum(r["单期平均净收益"] for r in lst) / n, 4),
            "达标期数/100元": round(sum(r["达标期数/100元"] for r in lst) / n, 6),
            "净收益/100元": round(sum(r["净收益/100元"] for r in lst) / n, 6),
            "饱和": any(r.get("饱和") for r in lst),
            "理论上限Kp": lst[0].get("理论上限Kp"),
            "随机基线": None,
        }
        p1 = lst[0].get("单注达标概率") or 0.0
        agg[a]["随机基线"] = round(1.0 - (1.0 - p1) ** K, 6) if p1 else None

    # 跨 seed 全距 vs 组间差：全距 > 组间差 ⇒ 差异在噪声内
    spread = {}
    for a, lst in runs.items():
        v = [r["达标期占比"] for r in lst]
        spread[a] = round(max(v) - min(v), 6)
    gaps = {}
    base = "tier_coverage" if "tier_coverage" in arms else arms[0]
    for a in arms:
        if a == base:
            continue
        d = agg[base]["达标期占比"] - agg[a]["达标期占比"]
        noise = max(spread[base], spread[a])
        gaps[f"{base} - {a}"] = {
            "差": round(d, 6),
            "跨seed全距": noise,
            "超过噪声": bool(abs(d) > noise),
        }

    paired: Dict[str, Any] = {}
    for a in arms:
        if a == base:
            continue
        try:
            paired[f"{base} vs {a}"] = paired_report(
                runs[base][0], runs[a][0], metric="≥10元", block=block, reps=reps,
                seed=seed)
        except Exception as e:                     # noqa: BLE001
            paired[f"{base} vs {a}"] = {"失败": str(e)}
    # 达标序列的配对（本策略主判据；paired_report 只认 序列 里的三个键）
    tier_paired: Dict[str, Any] = {}
    for a in arms:
        if a == base:
            continue
        sb = runs[base][0].get("序列_达标") or []
        sa = runs[a][0].get("序列_达标") or []
        if len(sb) == len(sa) and sb:
            d = [x - y for x, y in zip(sb, sa)]
            tier_paired[f"{base} vs {a}"] = {
                "配对期数": len(d),
                "差均值": round(sum(d) / len(d), 6),
                "符号检验": sign_test(sb, sa),
                "块bootstrap": block_bootstrap_ci(d, block=block, reps=reps, seed=seed),
            }

    sentinel = {a: agg[a]["单期平均中奖注数"] for a in arms}
    s_vals = list(sentinel.values())
    return {
        "彩种": lottery, "K": K, "目标": agg and runs[base][0].get("目标"),
        "seeds": list(seeds), "期数": runs[base][0]["期数"],
        "重选间隔": reselect_every, "重叠上限": cap,
        "臂": agg,
        "跨seed全距": spread,
        "组间差": gaps,
        "配对(≥10元)": paired,
        "配对(达标)": tier_paired,
        "哨兵_单期平均中奖注数": sentinel,
        "哨兵全距": round(max(s_vals) - min(s_vals), 6),
        "哨兵提示": ("各组合等可能 ⇒ 换选号不该改变单期平均中奖注数。"
                     "哨兵出现显著差异时先查实现缺陷/前视泄漏，不主张策略有效。"),
        "术语铁律": TERMINOLOGY_NOTE,
        "上界提示": ("达标期占比超过理论上限 Kp 或低于随机基线，都应优先怀疑"
                     "口径/实现错误，而不是宣布效应。"),
        "净收益口径": ("`净收益/100元` 及 `单期平均净收益` **只作报告，永不参与臂间比较**："
                       "双色球 K=200/500 期下三等期望命中仅 0.91 次，实测某臂 2 次、某臂 0 次，"
                       "一两次 3000 元就能拉开 2.5 pp 以上并翻转排序。"
                       "要看可比量请读 `净收益/100元_剔除最大一期`（剔除后三臂回到固定奖口径 ≈−76）。"),
    }
